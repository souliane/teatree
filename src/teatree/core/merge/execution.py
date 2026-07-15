"""The §17.4 keystone: preconditions orchestration, bound merge, post hook, merge_ticket_pr.

The only sanctioned path from ``IN_REVIEW`` → ``MERGED`` (BLUEPRINT §17.4 holds
the full spec). Raw ``gh pr merge`` / ``glab mr merge`` is mechanically refused
(``hook_router._BLOCKED_COMMANDS``); it would bypass the ledger update, the
HEAD/workstream attestation binding, the privacy scan, and ``mark_merged()``.

The transport (GitHub + GitLab) resolves via ``core.backend_registry`` so core
never imports ``teatree.backends`` (§17.6.2); this module keeps every verdict /
transient / head-moved / policy-refusal classification and the exact error
f-strings — the residual host-kind switch selects the classifier, never the
transport. The #928 lost-post-hook reconciliation and the #1813 transient retry
are documented on :func:`assert_merge_preconditions` / :func:`execute_bound_merge`
/ :func:`record_merge_and_advance`.
"""

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.apps import apps
from django.db import transaction
from django.utils import timezone
from django_fsm import TransitionNotAllowed

from teatree.core.merge.authorization import (
    MergePrecheck,
    PresentedApprovals,
    _assert_anti_vacuity,
    _assert_clear_authorized,
    _assert_rubric_satisfied,
    assert_merge_provenance_trusted,
    assert_no_active_review_lock,
    assert_review_verdict_gate,
)
from teatree.core.merge.ci_rollup import CodeHostQuery, attach_touched_paths
from teatree.core.merge.errors import MergePreconditionError, MergeReplayError, MergeTransientError
from teatree.core.merge.head_guard import restore_caller_branch
from teatree.core.merge.merge_response import _raise_bound_merge_failure
from teatree.core.merge.pr_slug_resolution import (
    _reconcile_slug_against_reviewed_sha,
    _resolve_host_kind,
    resolve_pr_repo_slug,
)
from teatree.core.merge.sha_bind import verify_sha_bound
from teatree.project import find_project_root
from teatree.utils.pr_ref import PrRef

if TYPE_CHECKING:
    from teatree.core.models import MergeClear

logger = logging.getLogger(__name__)


MERGE_TRANSIENT_ATTEMPTS = 3
MERGE_TRANSIENT_BASE_DELAY = 0.5


@dataclass(frozen=True, slots=True)
class MergeOutcome:
    pr_id: int
    slug: str
    merged_sha: str
    ticket_state: str


def _reconcile_if_already_merged(
    *,
    query: CodeHostQuery,
    live_sha: str,
) -> "MergePrecheck | None":
    """§928 reconciliation — the recovery path for a lost post-merge hook.

    Called only after the SHA re-check has passed (the head still equals
    ``reviewed_sha`` — a squash merge does not move the source-branch
    tip). If GitHub also reports the PR already MERGED, a prior attempt's
    irreversible merge LANDED but its post hook was lost (process kill /
    DB lock / rollback between :func:`execute_bound_merge` and
    :func:`record_merge_and_advance`). Re-issuing the merge would 405
    forever and the SHA gate can never self-heal — a permanent
    "merged-on-GitHub, not-in-FSM" brick. Because the head is still bound
    to the exact reviewed tree AND every guard in
    :func:`assert_merge_preconditions` (actionable / reviewer≠loop /
    substrate refusal) has already passed, completing the post hook
    idempotently against the existing merge commit is sound and weakens
    no guarantee. Returns ``None`` when the PR is not (yet) merged so the
    caller proceeds with the normal fresh-merge path.
    """
    merge_state = query.pr_merge_state()
    if not merge_state.is_merged:
        return None
    return MergePrecheck(verified_sha=live_sha, already_merged_sha=merge_state.merge_commit_oid or live_sha)


def assert_merge_preconditions(
    *,
    clear: object,
    executing_loop_identity: str,
    ref: PrRef,
    human_authorized: str = "",
    expedite_authorized: str = "",
) -> MergePrecheck:
    """Run the §17.4.3 loop validation in order; return the :class:`MergePrecheck`.

    Raises :class:`MergePreconditionError` on the first failed check. The
    durable-backlog re-escalation is the caller's responsibility (§17.4.3) —
    this function never self-issues a replacement CLEAR.

    §928 reconciliation: the substrate / reviewer-identity / actionable
    guards run FIRST (so a stale CLEAR can never be reconciled past
    maker≠checker or the substrate auto-merge refusal). Only then, if
    GitHub reports the PR already MERGED at the exact ``reviewed_sha``
    tree, the returned precheck signals ``needs_reconcile`` so the caller
    runs the post hook idempotently instead of re-issuing the merge — a
    lost post-hook becomes recoverable rather than a permanent brick.

    Substrate (step 5) is HELD for the owner — never covered by the standing
    grant, not even at ``autonomy = full``: it pings-and-holds (the loop edge
    DMs the owner). The ONLY thing that unlocks a substrate merge is a matching
    per-CLEAR ``human_authorized`` re-presented at merge time; the AGENT then
    executes through this same sanctioned transition (invariant 8). A substrate
    diff is detected by EITHER the ``blast_class`` label OR the live diff paths
    (:func:`attach_touched_paths`), so a mislabeled substrate change is still
    held. Non-substrate self-merges through the standing grant unchanged. The
    quality/safety floor (independent cold-review, reviewed-SHA bind, CI-green,
    not-draft, never-lockout, privacy scan) is untouched.
    """
    query = CodeHostQuery.for_ref(ref)
    slug, pr_id = ref.slug, ref.pr_id

    # Attach the live diff paths so the substrate authorization guard can detect
    # a mislabeled substrate diff (path-based classifier — invariant 4). A forge
    # error degrades to no paths: the path detector only WIDENS substrate over the
    # recorded ``blast_class``, never narrows it, so a missing diff never weakens
    # the label-based gate. Set BEFORE the authorize call so the substrate branch
    # reads it.
    attach_touched_paths(clear, query)

    authorized_clear = _assert_clear_authorized(
        clear=clear,
        executing_loop_identity=executing_loop_identity,
        slug=slug,
        pr_id=pr_id,
        approvals=PresentedApprovals(human=human_authorized, expedite=expedite_authorized),
    )

    # 2. SHA still matches — re-fetch the live head; it must equal reviewed_sha.
    live_sha = query.live_head_sha()
    if not live_sha:
        msg = f"could not resolve the live head SHA for {slug}#{pr_id} (§17.4.3 step 2)"
        raise MergePreconditionError(msg)
    if not verify_sha_bound(cleared_sha=authorized_clear.reviewed_sha, live_sha=live_sha):
        # Show full SHAs (not [:8] prefixes) so a length-mismatch or any other
        # silent difference is obvious in the diagnostic (#1162).
        reviewed = authorized_clear.reviewed_sha
        msg = (
            f"PR head moved: live={live_sha} (length={len(live_sha)}) != "
            f"reviewed={reviewed} (length={len(reviewed)}) — the CLEAR is stale "
            f"(force-push / new commits) or was issued with a truncated SHA. "
            f"Re-escalate; the loop never self-issues a replacement (§17.4.3 step 2)"
        )
        raise MergePreconditionError(msg)

    # §17.4.3 + #1829: bound to the just-verified ``live_sha`` so a force-push
    # invalidates the CLEAR and the attestation together (see _assert_anti_vacuity).
    _assert_anti_vacuity(authorized_clear, live_sha)

    # §17.4.3 + #2241: the rubric->verifier done-gate, bound to the same just-verified
    # ``live_sha`` — the ticket's acceptance-criteria rubric must be fully PASS by an
    # independent verifier at the head, or the merge is refused (see _assert_rubric_satisfied).
    _assert_rubric_satisfied(authorized_clear, live_sha)

    reconcile = _reconcile_if_already_merged(query=query, live_sha=live_sha)
    if reconcile is not None:
        return reconcile

    # 4. Not draft.
    if query.pr_is_draft():
        msg = f"{slug}#{pr_id} is in draft state — refusing to merge (§17.4.3 step 4)"
        raise MergePreconditionError(msg)

    # 3. CI still not FAILED — against the forge's LIVE rollup, not the saved
    # snapshot. Three-valued (green/pending/failed):
    #   * failed  — a real red verdict; ALWAYS refused. Expedite can never waive it
    #               (the anti-vacuity pin: even a fully-authorized expedite CLEAR
    #               with FAILED live checks is refused here).
    #   * pending — queued checks, no verdict; refused UNLESS the CLEAR carries a
    #               valid human-authorized waiver re-presented as ``expedite_authorized``
    #               AND still bound to the reviewed tree (``expedite_pending_waived_by``).
    #   * green   — proceeds unchanged.
    checks = query.required_checks_status()
    if checks == "failed":
        msg = (
            f"live required-checks for {slug}#{pr_id} are {checks!r}, not green — refusing to "
            f"merge (§17.4.3 step 3; the live list is the source of truth, not the CLEAR snapshot). "
            f"A FAILED required check is a verdict — expedite can never waive it"
        )
        raise MergePreconditionError(msg)
    if checks != "green":
        if not authorized_clear.expedite_pending_waived_by(expedite_authorized):
            msg = (
                f"live required-checks for {slug}#{pr_id} are {checks!r}, not green — refusing to "
                f"merge (§17.4.3 step 3). A queued (pending) required check merges ONLY via the "
                f"sanctioned human-authorized expedite waiver: `t3 <overlay> ticket merge <clear_id> "
                f"--expedite-authorized <recorded-id>` on a CLEAR issued with `--expedite-authorize` "
                f"and a `--local-ci-green-sha` bound to the reviewed tree"
            )
            raise MergePreconditionError(msg)
        return MergePrecheck(verified_sha=live_sha, expedited_by=expedite_authorized.strip())

    return MergePrecheck(verified_sha=live_sha)


def assert_not_draft(query: CodeHostQuery) -> None:
    """§17.4.3 step 4 floor: refuse the bound merge when the PR/MR is in draft state.

    The last-line not-draft gate at the merge chokepoint — re-reads the forge's
    LIVE draft flag so an open→draft flip in the TOCTOU window between a caller's
    snapshot and the irreversible PUT is refused here. A registered
    ``merge_keystone`` gate (:mod:`teatree.core.factory.chokepoint_registry`).
    """
    if query.pr_is_draft():
        msg = f"{query.ref.slug}#{query.ref.pr_id} is in draft state — refusing bound merge (§17.4.3 step 4)"
        raise MergePreconditionError(msg)


def assert_ci_not_failed(query: CodeHostQuery) -> None:
    """§17.4.3 step 3 floor: refuse the bound merge on a live FAILED required-checks verdict.

    The last-line CI-verdict gate at the merge chokepoint — re-reads the forge's
    LIVE rollup so a green→red flip in the TOCTOU window is refused here. A FAILED
    required check is a verdict expedite can NEVER waive, so it is refused
    unconditionally (the pending-waiver lives only in
    :func:`assert_merge_preconditions`, which the keystone runs first). A registered
    ``merge_keystone`` gate (:mod:`teatree.core.factory.chokepoint_registry`).
    """
    if query.required_checks_status() == "failed":
        msg = (
            f"live required-checks for {query.ref.slug}#{query.ref.pr_id} are failed — refusing bound merge "
            f"(§17.4.3 step 3; a FAILED required check is a verdict expedite can never waive)"
        )
        raise MergePreconditionError(msg)


def execute_bound_merge(
    *,
    ref: PrRef,
    expected_head_oid: str,
) -> str:
    """Squash-merge bound to ``expected_head_oid`` — fail closed on head drift.

    GitHub: ``PUT repos/<slug>/pulls/<n>/merge`` with ``sha=<oid>``. GitLab: ``PUT
    projects/<encoded>/merge_requests/<iid>/merge`` with ``sha=<oid>`` (409s on drift).

    If the forge reports the head moved, the merge is refused and raised as
    :class:`MergeHeadMovedError` — a failed check, never a retry-with-new-head
    (§17.4.3 "bind execution to the exact verified SHA, fail closed").

    A transient/empty-JSON/network/5xx forge response (#1813 — the #1804
    ``unexpected end of JSON input`` window) is the forge momentarily
    failing to answer, NOT a verdict: it is auto-retried up to
    :data:`MERGE_TRANSIENT_ATTEMPTS` times with exponential backoff before
    raising :class:`MergeTransientError`. Because the failure is raised
    BEFORE the post hook, the single-use CLEAR is never consumed — a retry
    of the SAME CLEAR can merge. Before each retry the PR's merge state is
    re-probed: a transient response whose merge ACTUALLY LANDED at the
    bound SHA returns the existing merge commit so the caller runs the post
    hook idempotently instead of re-issuing the (then-405-bricking) merge.
    A policy refusal (not-mergeable / required-checks / 405 / 422) and a
    head-moved are NOT transient — they raise on the first attempt. Before the
    retry loop, five gates run — the single chokepoint BOTH merge paths cross
    (the keystone via ``assert_merge_preconditions`` AND the solo-overlay bypass
    via ``merge_pr_squash_bound`` with NO preconditions run): ``assert_review_verdict_gate``
    (#2829), ``assert_no_active_review_lock`` (#1405), ``assert_merge_quality_verdict``
    (north-star PR-4 — a directive keystone / opted-in ordinary ticket needs a clean
    recorded merge-quality verdict at the shipped head), and the #18 not-draft +
    FAILED-live-CI floor. The latter re-reads the forge's LIVE state at the merge
    chokepoint so a green→red / open→draft flip in the TOCTOU window between a
    caller's snapshot and this PUT is refused here — the solo lane had NO such
    re-check despite the sweep docstring claiming one. A FAILED required check is
    a verdict expedite can NEVER waive, so it is refused unconditionally (no
    expedite plumbing at this chokepoint; the pending-waiver lives only in
    ``assert_merge_preconditions``, which the keystone runs first).
    """
    query = CodeHostQuery.for_ref(ref)
    slug, pr_id = ref.slug, ref.pr_id
    # #3244 defence-in-depth: the solo-overlay bypass (``merge_pr_squash_bound`` →
    # here) reaches this shared chokepoint with NO keystone preconditions run, so
    # the provenance gate must fire HERE too — otherwise a fork PR could auto-merge
    # via the bypass path even though the keystone (below) refuses it.
    assert_merge_provenance_trusted(slug=slug, pr_id=pr_id, host_kind=ref.host_kind)
    assert_review_verdict_gate(slug=slug, pr_id=pr_id, head_sha=expected_head_oid)
    assert_no_active_review_lock(slug=slug, pr_id=pr_id)
    # north-star PR-4: merely-green-but-not-well-engineered does not merge. A
    # directive keystone (and, under `require_merge_quality_verdict`, an ordinary
    # ticket) is refused unless a clean recorded merge-quality CriticVerdict
    # (test_value + cleanliness) covers this exact shipped head. Lazy-imported like
    # the other keystone gates so core.merge stays free of an import-time gate edge.
    # The gate import is function-scoped on purpose: a module-level core.merge ->
    # core.gates edge is a tach cycle (core.gates already imports core.merge.errors),
    # so it stays deferred like the sibling merge-precondition gates.
    from teatree.core.gates import merge_quality_gate  # noqa: PLC0415 avoids a core.merge/core.gates cycle

    merge_quality_gate.assert_merge_quality_verdict(slug=slug, pr_id=pr_id, head_sha=expected_head_oid)
    assert_not_draft(query)
    assert_ci_not_failed(query)
    for attempt in range(MERGE_TRANSIENT_ATTEMPTS):
        if attempt > 0:
            landed = _already_merged_at(query=query, expected_head_oid=expected_head_oid)
            if landed:
                return landed
            time.sleep(MERGE_TRANSIENT_BASE_DELAY * (2 ** (attempt - 1)))
        try:
            return _attempt_bound_merge(query=query, expected_head_oid=expected_head_oid)
        except MergeTransientError as exc:
            if attempt == MERGE_TRANSIENT_ATTEMPTS - 1:
                raise
            logger.info(
                "merge_execution: transient forge response on merge attempt %d/%d for %s#%s — %s",
                attempt + 1,
                MERGE_TRANSIENT_ATTEMPTS,
                slug,
                pr_id,
                exc,
            )
    msg = f"merge of {slug}#{pr_id} exhausted {MERGE_TRANSIENT_ATTEMPTS} transient retries"  # pragma: no cover
    raise MergeTransientError(msg)  # pragma: no cover — the final attempt re-raises before the loop can fall through


def _already_merged_at(*, query: CodeHostQuery, expected_head_oid: str) -> str:
    """The existing merge commit when the PR/MR is ALREADY merged at ``expected_head_oid``.

    A transient response may mask a merge that actually LANDED on the forge
    (the body was truncated, not the action). Re-probing before the next
    retry detects that and returns the existing merge commit (or the bound
    SHA when the forge exposes no merge-commit oid), so the caller runs the
    idempotent post hook rather than re-issuing a merge the forge would now
    405. Returns ``""`` when the PR/MR is not (yet) merged.
    """
    merge_state = query.pr_merge_state()
    if not merge_state.is_merged:
        return ""
    return merge_state.merge_commit_oid or expected_head_oid


def _attempt_bound_merge(*, query: CodeHostQuery, expected_head_oid: str) -> str:
    """One bound-merge attempt; raises :class:`MergeTransientError` on a retryable response.

    The backend's :meth:`CodeHostBackend.merge_pr_squash_bound` runs the
    PUT and returns the raw :class:`ForgeMergeResult`; core classifies it
    (head-moved / transient / policy refusal) and raises the typed error with
    the forge-specific f-string here, so the byte-for-byte error parity the
    keystone tests pin is unchanged while the transport lives in the backend.
    """
    slug, pr_id = query.ref.slug, query.ref.pr_id
    result = query.backend.merge_pr_squash_bound(
        slug=slug,
        pr_id=pr_id,
        expected_head_oid=expected_head_oid,
    )
    if result.returncode != 0:
        _raise_bound_merge_failure(
            result=result,
            slug=slug,
            pr_id=pr_id,
            expected_head_oid=expected_head_oid,
            host_kind=query.ref.host_kind,
        )
    return result.merged_sha or expected_head_oid


def record_merge_and_advance(
    *,
    clear: object,
    merged_sha: str,
    required_checks_status: str,
    expedited_by: str = "",
    repo_slug: str = "",
) -> str:
    """Post hook: consume CLEAR, write audit, supersede siblings, ``mark_merged()``.

    All in ONE ``transaction.atomic()`` so the FSM advance and the durable
    merge record land atomically (the §4 worker-enqueue / sync-atomicity
    invariant): a crash *within* this post hook rolls back the whole
    transaction, leaving the CLEAR unconsumed and the FSM unmoved — a
    re-runnable state. A crash *between* the irreversible GitHub merge and
    this hook also leaves the CLEAR unconsumed, but the PR is now merged on
    GitHub; that case is recovered by the #928 reconciliation in
    :func:`assert_merge_preconditions` (the retry detects "already merged
    at ``reviewed_sha``" and runs this hook idempotently instead of
    re-issuing the merge). Returns the resulting ticket state.

    ``repo_slug`` is the #1335-reconciled ``owner/repo`` the caller merged
    against; it is stamped on the ``MergeAudit`` (#19) so the S1/S3 signal joins
    read the merge-time truth first instead of re-resolving the CLEAR's offline
    workstream slug. Empty only for a legacy/direct caller — the signal resolver
    falls back to ``resolve_pr_repo_slug`` for a blank audit.

    §15: a head-move re-review issues a fresh CLEAR at the new SHA, leaving the
    older sibling unconsumed. Consuming ONE via a merge supersedes every sibling
    unconsumed CLEAR for the same ``(slug, pr_id)`` in the same atomic block under
    the row lock, so a stale orphan can no longer ratchet S4 hard-red forever. No
    ``ReviewVerdict`` is moved: each sibling's verdict persists at its own
    reviewed_sha and S3 counts it regardless of SHA, so there is no verdict-copy
    path to hand-roll (GM-4's ``carry_forward`` is the primitive if one is ever
    needed).

    The atomic block is wrapped in :func:`retry_on_locked` (#1520): a transient
    ``database is locked`` from a concurrent canonical-DB writer must not abort
    the merge keystone mid-flight. A retry re-opens the transaction, re-reads
    the CLEAR ``select_for_update``-locked, and re-asserts the single-use
    guard, so it consumes the CLEAR exactly once and never double-merges (the
    irreversible GitHub merge already ran before this hook; only this
    idempotent DB write retries).
    """
    from teatree.core.modelkit.db_retry import retry_on_locked  # noqa: PLC0415 — deferred: call-time import, kept lazy
    from teatree.core.models import MergeClear  # noqa: PLC0415 — deferred: ORM import needs the app registry

    if not isinstance(clear, MergeClear):  # pragma: no cover - guarded by caller
        msg = "record_merge_and_advance requires a MergeClear instance"
        raise MergePreconditionError(msg)

    merge_audit_model = apps.get_model("core", "MergeAudit")

    def _consume_and_advance() -> str:
        with transaction.atomic():
            locked = MergeClear.objects.select_for_update().get(pk=clear.pk)
            # Re-assert single-use UNDER the row lock. ``assert_merge_preconditions``
            # checked ``is_actionable()`` unlocked; two concurrent executors that
            # both passed it must not both consume — exactly one wins this
            # serialized re-check, the loser raises ``MergeReplayError`` and
            # writes no audit / does not advance the FSM.
            if locked.consumed_at is not None:
                msg = (
                    f"MergeClear {locked.pk} ({locked.slug}#{locked.pr_id}) was already "
                    f"consumed at {locked.consumed_at.isoformat()} — concurrent double-merge "
                    f"refused under the row lock (§17.4.3 single-use replay defence)"
                )
                raise MergeReplayError(msg)
            locked.consumed_at = timezone.now()
            locked.save(update_fields=["consumed_at"])
            merge_audit_model.objects.create(
                clear=locked,
                merged_sha=merged_sha,
                required_checks_status=required_checks_status,
                expedited_by=expedited_by,
                repo_slug=repo_slug,
            )
            # §15: supersede every sibling unconsumed CLEAR for the same PR —
            # re-review at a moved head issues a fresh CLEAR at the new SHA,
            # leaving the older one unconsumed. Once THIS merge consumes one, its
            # siblings are no longer a stalled merge, so consume them in the same
            # atomic block (single serialized UPDATE) under the row lock.
            MergeClear.objects.filter(
                slug=locked.slug,
                pr_id=locked.pr_id,
                consumed_at__isnull=True,
            ).exclude(pk=locked.pk).update(consumed_at=locked.consumed_at)
            ticket = locked.ticket
            if ticket is None:
                return ""
            # Bind the phase attestation to the merged HEAD/workstream it was
            # earned against (the §17.6 enforcement candidate (7), absorbed
            # here): the canonical phase session records the SHA that actually
            # landed, so a later stale-workstream attestation cannot be reused
            # against a different HEAD.
            session = ticket.resolve_phase_session(agent_id="merge-loop")
            session.visit_phase("merged", agent_id=f"merge-loop@{merged_sha[:12]}")
            # #1343: state-complete reconcile. An authorised, audited PR-merge
            # is the authority — every pre-merged state (NOT_STARTED through
            # IN_REVIEW, plus SHIPPED) must advance to MERGED. RETROSPECTED/
            # DELIVERED are past MERGED and stay where they are; IGNORED is
            # abandoned. The original ``state in {in_review, merged}`` guard
            # left STARTED tickets visibly stuck on the statusline after their
            # PR merged (#1324 follow-up). The FSM source-set on
            # ``reconcile_merged`` is the single source of truth — catching
            # ``TransitionNotAllowed`` lets the source list evolve in one
            # place (the model) without a parallel guard here.
            try:
                ticket.reconcile_merged()
            except TransitionNotAllowed:
                logger.info(
                    "merge keystone: ticket %s state=%s is past MERGED; FSM unchanged",
                    ticket.pk,
                    ticket.state,
                )
            else:
                ticket.save()
            return ticket.state

    return retry_on_locked(_consume_and_advance)


def merge_ticket_pr(
    *,
    clear: object,
    executing_loop_identity: str,
    human_authorized: str = "",
    expedite_authorized: str = "",
) -> MergeOutcome:
    """The full keystone transition: pre-condition → atomic merge → post hook.

    This is what the ``t3 <overlay> ticket merge`` CLI / durable loop calls.
    Any :class:`MergePreconditionError` propagates unchanged so the caller can
    write the durable-backlog re-escalation (§17.4.3) and leave the FSM
    untouched — the transition is all-or-nothing.

    ``human_authorized`` is empty for every loop-driven merge (the loop never
    auto-merges substrate). For a substrate CLEAR the recorded human approval
    id is re-presented here and **the agent executes** the merge through this
    same sanctioned transition (invariant 8 — approval is the gate, the agent
    is always the executor) — see :func:`assert_merge_preconditions`.

    ``expedite_authorized`` is likewise empty for every loop-driven merge (the
    loop never auto-expedites). For an expedite CLEAR with live PENDING checks
    the recorded expedite authoriser is re-presented here to waive the pending
    (never a FAILED) required check — a distinct key from ``human_authorized`` so
    the substrate hold and the pending waiver can never cross-unlock.
    """
    from teatree.core.models import MergeClear  # noqa: PLC0415 — deferred: ORM import needs the app registry

    if not isinstance(clear, MergeClear):
        msg = "merge_ticket_pr requires a MergeClear instance"
        raise MergePreconditionError(msg)

    # #2383: the keystone runs from inside the primary clone; the cross-repo
    # SHA-recovery probe (or any future local tree read) must never leave that
    # clone on a detached HEAD at the merged PR branch — restore the caller's
    # checked-out ref around the whole transition, even on a refused merge.
    with restore_caller_branch(_caller_repo_root()):
        return _merge_ticket_pr_inner(
            clear=clear,
            executing_loop_identity=executing_loop_identity,
            human_authorized=human_authorized,
            expedite_authorized=expedite_authorized,
        )


def _caller_repo_root() -> str | None:
    """The primary-clone path the keystone is invoked from, or ``None``.

    The same project root :func:`pr_slug_resolution._project_repo_slug` resolves
    ``origin`` against — the cwd repo whose HEAD a local probe checkout could
    move (#2383). ``None`` (non-source install / no resolvable root) makes the
    head guard a no-op.
    """
    root = find_project_root()
    return str(root) if root is not None else None


def _merge_ticket_pr_inner(
    *,
    clear: "MergeClear",
    executing_loop_identity: str,
    human_authorized: str,
    expedite_authorized: str = "",
) -> MergeOutcome:
    slug = resolve_pr_repo_slug(clear)
    pr_id = clear.pr_id
    host_kind = _resolve_host_kind(clear)
    slug = _reconcile_slug_against_reviewed_sha(
        initial_slug=slug,
        pr_id=pr_id,
        reviewed_sha=str(getattr(clear, "reviewed_sha", "") or ""),
        host_kind=host_kind,
    )
    ref = PrRef(slug=slug, pr_id=pr_id, host_kind=host_kind)
    assert_merge_provenance_trusted(slug=slug, pr_id=pr_id, host_kind=host_kind)
    precheck = assert_merge_preconditions(
        clear=clear,
        executing_loop_identity=executing_loop_identity,
        ref=ref,
        human_authorized=human_authorized,
        expedite_authorized=expedite_authorized,
    )
    if precheck.needs_reconcile:
        # §928: a prior attempt's irreversible merge already landed; only
        # its post hook was lost. Do NOT re-issue the merge (the forge
        # would 405 forever). Complete the transition idempotently against
        # the existing merge commit — the single-use CLEAR is still
        # consumed exactly once under the row lock in
        # record_merge_and_advance, so this neither double-merges nor
        # weakens the replay defence.
        merged_sha = precheck.already_merged_sha
        reconciled = True
    else:
        merged_sha = execute_bound_merge(ref=ref, expected_head_oid=precheck.verified_sha)
        reconciled = False
    checks = CodeHostQuery.for_ref(ref).required_checks_status()
    state = record_merge_and_advance(
        clear=clear,
        merged_sha=merged_sha,
        required_checks_status=checks,
        expedited_by=precheck.expedited_by,
        repo_slug=slug,
    )
    logger.info(
        "merge keystone: %s#%s %s at %s; ticket state=%s",
        slug,
        pr_id,
        "reconciled (lost post-hook recovered)" if reconciled else "merged",
        merged_sha[:8],
        state or "(no ticket)",
    )
    return MergeOutcome(pr_id=pr_id, slug=slug, merged_sha=merged_sha, ticket_state=state)
