"""§17.4.3 identity/substrate authorization guards + the anti-vacuity wrapper.

The result type :class:`MergePrecheck` and the guard functions
:func:`_assert_clear_authorized` / :func:`_assert_anti_vacuity` that
:mod:`execution` runs before it binds the irreversible merge.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from teatree.core.merge.ci_rollup import CodeHostQuery
from teatree.core.merge.errors import MergePreconditionError
from teatree.core.models.mr_review_lock import MRReviewLock
from teatree.core.models.review_verdict import HeadVerdictState, ReviewVerdict
from teatree.core.review.author_trust import classify_pr_provenance
from teatree.utils.pr_ref import PrRef

if TYPE_CHECKING:
    from teatree.core.models import MergeClear


@dataclass(frozen=True, slots=True)
class MergePrecheck:
    """Outcome of :func:`assert_merge_preconditions`.

    ``verified_sha`` is the SHA the merge binds to (``expected_head_oid``).
    ``already_merged_sha`` is non-empty only when the §928 reconciliation
    fired: GitHub reports the PR already MERGED at the exact reviewed tree
    (a lost post-hook), so the irreversible merge must be SKIPPED and the
    post hook run idempotently against the existing merge commit.
    ``expedited_by`` is non-empty only when the merge proceeded on PENDING
    live checks via the human-authorized expedite waiver (§17.4.3 / PR-07) —
    the authoriser stamped onto the ``MergeAudit`` row.
    """

    verified_sha: str
    already_merged_sha: str = ""
    expedited_by: str = ""

    @property
    def needs_reconcile(self) -> bool:
        return bool(self.already_merged_sha)


@dataclass(frozen=True, slots=True)
class PresentedApprovals:
    """The two orthogonal approval ids re-presented at ``ticket merge`` (§17.4.3).

    ``human`` unlocks a substrate CLEAR (``--human-authorized``); ``expedite``
    waives a PENDING (never FAILED) required check on an expedite CLEAR
    (``--expedite-authorized``). Kept distinct so the substrate hold and the
    pending waiver can never cross-unlock (one presented token unlocks exactly
    one relaxation).
    """

    human: str = ""
    expedite: str = ""


def _assert_clear_actionable(clear: object, *, slug: str, pr_id: int) -> "MergeClear":
    """Step 1: a real, fully-populated, unconsumed :class:`MergeClear` row exists.

    Narrows the duck-typed *clear* to a :class:`MergeClear` — the ONE runtime
    boundary check the deeper guards then rely on (they take the narrowed type).
    Raises :class:`MergePreconditionError` when no actionable CLEAR backs the PR.
    """
    from teatree.core.models import MergeClear  # noqa: PLC0415 — deferred: ORM import needs the app registry

    if not isinstance(clear, MergeClear):
        msg = f"no MergeClear row for {slug}#{pr_id} — refusing to merge (§17.4.3 step 1)"
        raise MergePreconditionError(msg)
    if not clear.is_actionable():
        msg = (
            f"MergeClear for {slug}#{pr_id} is not actionable (missing fields or already "
            f"consumed) — treated as absent (§17.4.2/§17.4.3 step 1)"
        )
        raise MergePreconditionError(msg)
    return clear


def _assert_verdict_not_smuggled(clear: "MergeClear", *, slug: str, pr_id: int, expedite: str) -> None:
    """The recorded reviewer verdict must be merge-safe (the raw-ORM-smuggle double-guard).

    ``MergeClear.issue()`` rejects a FAILED verdict at issue time and a PENDING
    one without a bound expedite waiver, but a row written directly via
    ``.objects.create()`` (fixture / migration / non-factory ORM path) could
    smuggle either past it. Re-check here so the live-CI re-check downstream can
    never stamp green over the reviewer's recorded HOLD when CI self-flips green —
    the green-over-HOLD class (§17.8 clause 3: the checker's recorded verdict is
    authoritative). FAILED is refused unconditionally; PENDING is accepted ONLY
    when the row carries a valid bound expedite waiver re-presented at merge time
    (mirroring ``expedite_pending_waived_by`` at the live-check step).
    """
    if clear.gh_verify_result == clear.VerifyResult.FAILED:
        msg = (
            f"MergeClear for {slug}#{pr_id} records gh_verify_result=failed — a FAILED required "
            f"check is a real red verdict; expedite can never waive it, so it can never authorize "
            f"a merge regardless of the live CI rollup (§17.4.2 / §17.8 clause 3)"
        )
        raise MergePreconditionError(msg)
    if clear.gh_verify_result != clear.VerifyResult.GREEN and not clear.expedite_pending_waived_by(expedite):
        msg = (
            f"MergeClear for {slug}#{pr_id} records gh_verify_result "
            f"({clear.gh_verify_result!r}), not green — the reviewer recorded a HOLD at the reviewed "
            f"tree. A PENDING (queued) verdict authorizes a merge ONLY via a re-presented, "
            f"tree-bound expedite waiver (`t3 <overlay> ticket merge <id> --expedite-authorized "
            f"<recorded-id>` on an expedite CLEAR); no valid waiver was presented (§17.4.2 / §17.8 "
            f"clause 3)"
        )
        raise MergePreconditionError(msg)


def _assert_reviewer_independent(clear: "MergeClear", *, executing_loop_identity: str) -> None:
    """The reviewer identity must be an independent cold reviewer (§17.8 clause 3).

    Two guards: the reviewer must not BE the executing loop (the loop cannot
    rubber-stamp its own CLEAR), and the reviewer must not be a maker/coding-
    agent/loop non-reviewer role. ``MergeClear.issue()`` rejects the latter at
    issue time via the same shared ``is_non_reviewer_role`` helper, but a row
    written directly via ``.objects.create()`` (fixture, migration, or the pk-load
    path in ``ticket.py``) would otherwise smuggle a self-attesting maker through
    the equality check, so the issue-time and merge-time gates re-check identically
    and cannot drift apart (codex #1282 finding 1 / #1283).
    """
    from teatree.core.models.merge_clear import is_non_reviewer_role  # noqa: PLC0415 — deferred: ORM/app-registry

    if clear.reviewer_identity.strip() == executing_loop_identity.strip():
        msg = (
            f"MergeClear reviewer_identity ({clear.reviewer_identity!r}) equals the "
            f"executing loop identity — a CLEAR must be issued by an independent "
            f"cold reviewer, not self-issued (§17.8 clause 3)"
        )
        raise MergePreconditionError(msg)
    if is_non_reviewer_role(clear.reviewer_identity):
        msg = (
            f"MergeClear reviewer_identity ({clear.reviewer_identity!r}) is a "
            f"maker/coding-agent/loop non-reviewer role — a CLEAR must be issued "
            f"by an independent cold reviewer, not self-attested (§17.8 clause 3)"
        )
        raise MergePreconditionError(msg)


def _assert_substrate_authorized(clear: "MergeClear", *, slug: str, pr_id: int, human: str) -> None:
    """Step 5: substrate is held for the owner unless a per-PR human sign-off unlocks it.

    Two guards. First: the human-substrate escape is substrate-only — presenting
    ``--human-authorized`` against a non-substrate CLEAR is refused outright so it
    can never short-circuit independent loop review of a logic/docs PR (invariant 8
    / §17.4.1). Second: a substrate-class PR is draft-locked and requires a recorded
    PER-PR human sign-off (invariant 4 / §17.4.3 step 5). By default substrate is
    NOT covered by the overlay's standing grant — not even at ``autonomy = full``:
    the owner's directive is that substrate (merge keystone, architecture spec,
    governance doc) PINGS-and-HOLDS. The one opt-in that lifts the hold is the
    explicit, default-off ``substrate_self_signoff`` config setting (#3223), which
    lets ``_overlay_grants_standing_substrate_signoff`` cover a substrate clear on a
    ``full`` (solo-owned) overlay. When neither the per-CLEAR ``human_authorizer``
    nor that standing grant is satisfied the held clear raises below, which the loop
    edge routes to the substrate-hold Slack ping; the AGENT still executes the
    authorized merge through this same SHA-bound, audited transition. The
    quality/safety floor is untouched; non-substrate changes self-merge unchanged.
    """
    presented = human.strip()
    if presented and not clear.is_substrate():
        msg = (
            f"--human-authorized presented for non-substrate MergeClear "
            f"({slug}#{pr_id}, blast_class={clear.blast_class}); the recorded-human-"
            f"approval path is substrate-only — a logic/docs CLEAR merges through "
            f"the loop, not via a human-approval escape hatch (invariant 8 / §17.4.1)"
        )
        raise MergePreconditionError(msg)

    if (
        clear.is_substrate()
        and not clear.human_merge_authorized_by(presented)
        and not _overlay_grants_standing_substrate_signoff(clear, resolved_slug=slug)
    ):
        detail = (
            "no human authoriser recorded on the CLEAR — substrate is held for the owner, never auto-merged"
            if not clear.human_authorizer
            else f"presented authoriser != recorded ({clear.human_authorizer!r})"
            if presented
            else "no --human-authorized presented at merge time"
        )
        msg = (
            f"MergeClear for {slug}#{pr_id} is blast_class=substrate — substrate "
            f"changes are held for the owner and are draft-locked (invariant 4); the "
            f"loop never auto-merges them, not even at autonomy=full (§17.4.3 step 5). "
            f"{detail.capitalize()}. The sanctioned path: issue `t3 <overlay> ticket "
            f"clear … --blast-class substrate --human-authorize <id>` (a per-PR "
            f"recorded approval), then the agent executes `t3 <overlay> ticket "
            f"merge <clear_id> --human-authorized <id>`"
        )
        raise MergePreconditionError(msg)


def _assert_clear_authorized(
    *,
    clear: object,
    executing_loop_identity: str,
    slug: str,
    pr_id: int,
    approvals: PresentedApprovals | None = None,
) -> "MergeClear":
    """The §17.4.3 identity/substrate authorization guards (steps 1 + 5), in order.

    Split out of :func:`assert_merge_preconditions` so the orchestration there
    reads as the ordered §17.4.3 sequence (authorize → SHA → reconcile → draft →
    checks). This function is itself the ordered composition of the four guard
    facets — actionable → verdict-not-smuggled → reviewer-independent →
    substrate-authorized — each raising :class:`MergePreconditionError` on the
    first failure, so the refusal ORDER is the visible sequence below. Returns the
    narrowed :class:`MergeClear` on success. ``approvals`` defaults to none
    presented (a loop-driven merge presents neither key).
    """
    approvals = approvals or PresentedApprovals()
    narrowed = _assert_clear_actionable(clear, slug=slug, pr_id=pr_id)
    _assert_verdict_not_smuggled(narrowed, slug=slug, pr_id=pr_id, expedite=approvals.expedite)
    _assert_reviewer_independent(narrowed, executing_loop_identity=executing_loop_identity)
    _assert_substrate_authorized(narrowed, slug=slug, pr_id=pr_id, human=approvals.human)
    return narrowed


def _resolve_clear_overlay_name(clear: "MergeClear", *, resolved_slug: str = "") -> str:
    """The overlay name to resolve autonomy against for *clear* — by REPO IDENTITY.

    The merge-approval gate is a property of the **repo**, not of whatever
    overlay token a ticket happens to carry. A repo's OWNING overlay
    (:func:`infer_overlay_for_url` over every overlay's ``get_workspace_repos``)
    is authoritative — a repo resolves to its OWNING overlay even when the
    linked ticket was mis-stamped with a different overlay at creation.
    Resolving the stored ``ticket.overlay`` first inverted this: a ticket
    created while the agent was typed as a *different* overlay (the
    ``T3_OVERLAY_NAME`` the CLI bridge stamps, or a loop scanner setting
    ``ticket.overlay = self.overlay_name``) carried the WRONG overlay, so a PR
    on a repo governed by a ``full`` overlay was evaluated under a below-full
    overlay and refused. This is the name-collision trap: two overlays can carry
    similar names while owning disjoint repo sets — the repo's owning overlay,
    not the typed token, decides the gate.

    Resolution order, first non-empty wins:

    1.  :func:`infer_overlay_for_url` on the CLEAR's stored ``slug`` — the repo's
        OWNING overlay, authoritative. Resolves only when the stored slug is an
        ``owner/repo`` (the merge-authorization path stores ``owner/repo``).
    2.  :func:`infer_overlay_for_url` on *resolved_slug* — the real
        ``owner/repo`` the merge keystone recovered for this CLEAR
        (:func:`resolve_pr_repo_slug` →
        :func:`_reconcile_slug_against_reviewed_sha`). The loop routinely
        issues a ticket-less substrate CLEAR whose stored ``slug`` is a *branch
        name* (``merge-candidate-working-repos``), not ``owner/repo`` — step 1
        returns ``""`` for it, so this step resolves the SAME repo the bound
        merge targets.
    3.  ``clear.ticket.overlay`` — the stored token, the LAST resort. Used only
        when repo identity is inconclusive (no overlay claims either slug, e.g.
        a repo not yet declared in any overlay's ``workspace_repos``), so an
        existing attribution is never discarded for a blank inference.

    Returns ``""`` when no source resolves an overlay (the fail-closed default).
    """
    from teatree.core.overlay_loader import infer_overlay_for_url  # noqa: PLC0415 — deferred: call-time import

    from_stored = infer_overlay_for_url(str(getattr(clear, "slug", "") or "")).strip()
    if from_stored:
        return from_stored
    from_recovered = infer_overlay_for_url(resolved_slug.strip()).strip()
    if from_recovered:
        return from_recovered
    return str(getattr(getattr(clear, "ticket", None), "overlay", "") or "").strip()


def _overlay_grants_standing_substrate_signoff(clear: "MergeClear", *, resolved_slug: str = "") -> bool:
    """Whether the overlay's standing grant covers this per-PR sign-off (invariant 4 carve-out).

    A SUBSTRATE clear is held for the owner by default (the #2727 posture): even
    at ``autonomy = full`` the standing grant does not remove its per-PR human
    sign-off, so the held CLEAR raises the same MergePreconditionError the loop
    edge routes to the substrate-hold Slack ping. The one config seam that lifts
    that hold is the explicit, default-off ``substrate_self_signoff`` setting
    (#3223): on an overlay standing at ``autonomy = full`` (the solo-owned tier)
    with the setting on, a substrate CLEAR self-authorizes exactly as a
    non-substrate clear does. This changes only WHO authorizes the sign-off — the
    quality/safety floor (independent cold review, reviewed-SHA bind, CI-green,
    not-draft, maker≠checker, anti-vacuity) still runs. The ``full`` tier gate is
    kept so a below-full overlay never self-merges substrate even with the setting
    on.

    The remaining resolution (the standing grant for a NON-substrate clear —
    ``autonomy = full`` OR an explicit ``require_human_approval_to_merge = false``
    on a non-collaborative tier, the ``notify`` tier excluded, #2666) is retained
    so the gate stays a single named contract; non-substrate clears do not reach
    this function from the substrate-only call site, so for them it is moot.

    *resolved_slug* is the real ``owner/repo`` the merge keystone recovered for
    this CLEAR (threaded from :func:`assert_merge_preconditions`'s ``slug``
    kwarg) — see :func:`_resolve_clear_overlay_name`.
    """
    from teatree.config import Autonomy, get_effective_settings  # noqa: PLC0415 — deferred: call-time import, kept lazy

    overlay_name = _resolve_clear_overlay_name(clear, resolved_slug=resolved_slug)
    if not overlay_name:
        return False
    settings = get_effective_settings(overlay_name=overlay_name)
    # Substrate holds for the owner by default (#2727); the config seam is the
    # only opt-in that lifts it, and only on a solo-owned (``full``) overlay.
    if clear.is_substrate():
        return settings.substrate_self_signoff and settings.autonomy is Autonomy.FULL
    if settings.autonomy is Autonomy.FULL:
        return True
    # The collaborative ``notify`` tier collapses the merge-approval gate to
    # ``false`` too, but merges only after a colleague approval — its ``false``
    # is a tier side effect, never the owner's self-merge grant.
    if settings.autonomy is Autonomy.NOTIFY:
        return False
    return settings.require_human_approval_to_merge is False


def _assert_anti_vacuity(clear: "MergeClear", head_sha: str) -> None:
    """Refuse a merge whose CLEAR ticket lacks a SHA-bound anti-vacuity proof (#1829).

    NO-OP when ``require_anti_vacuity_attestation`` is off (opt-in default) or
    the CLEAR carries no ticket (the attestation lives on the ticket's durable
    ``extra``). The :class:`AntiVacuityAttestationError` raised on a block is
    re-wrapped as a :class:`MergePreconditionError` so the merge command's
    single re-escalation path surfaces it (the loop never self-issues a
    replacement CLEAR).
    """
    from teatree.core.gates.anti_vacuity_gate import (  # noqa: PLC0415 — deferred: call-time import, kept lazy
        AntiVacuityAttestationError,
        check_anti_vacuity_attestation,
    )

    ticket = clear.ticket
    if ticket is None:
        return
    try:
        check_anti_vacuity_attestation(ticket, head_sha, transition="merge")
    except AntiVacuityAttestationError as exc:
        raise MergePreconditionError(str(exc)) from exc


def _assert_rubric_satisfied(clear: "MergeClear", head_sha: str) -> None:
    """Refuse a merge whose CLEAR ticket's rubric is not fully PASS at ``head_sha`` (#2241).

    NO-OP when ``require_rubric_verification`` is off (opt-in default) or the CLEAR
    carries no ticket (the rubric is FK'd to the ticket). The
    :class:`RubricNotSatisfiedError` raised on a block is re-wrapped as a
    :class:`MergePreconditionError` so the merge command's single re-escalation
    path surfaces it (the loop never self-issues a replacement CLEAR). Sibling of
    :func:`_assert_anti_vacuity`; called immediately after it, bound to the same
    just-verified live head SHA so a force-push invalidates the CLEAR, the
    attestation, and the rubric grade together.
    """
    from teatree.core.gates.rubric_gate import (  # noqa: PLC0415 — deferred: call-time import, kept lazy
        RubricNotSatisfiedError,
        check_rubric_satisfied,
    )

    ticket = clear.ticket
    if ticket is None:
        return
    try:
        check_rubric_satisfied(ticket, head_sha, transition="merge")
    except RubricNotSatisfiedError as exc:
        raise MergePreconditionError(str(exc)) from exc


def assert_review_verdict_gate(*, slug: str, pr_id: int, head_sha: str) -> None:
    """Refuse the merge unless the effective verdict at the live head is merge_safe (#2829).

    The single chokepoint :func:`teatree.core.merge.execution.execute_bound_merge`
    runs this at its top, so NEITHER autonomous merge path — the keystone CLEAR
    path nor the solo-overlay bypass — can reach the forge squash PUT without a
    recorded INDEPENDENT cold-review (a non-self-attested
    :class:`~teatree.core.models.review_verdict.ReviewVerdict`, since
    ``ReviewVerdict.record`` forbids a maker/coding-agent/loop reviewer) that
    vouches for the EXACT live head. ``ReviewVerdict.reviewed_sha`` +
    ``is_stale_at`` give the head-SHA bind for free: a force-push moves the head,
    every prior verdict (PASS and HOLD) goes stale, and the gate fails closed.

    Newest-wins semantic (the user's chosen rule B): a later merge_safe overrides
    an earlier HOLD, an even-later HOLD re-blocks. The two refusal classes carry
    distinct messages — requirement (a): no non-stale merge_safe at the head
    (fail closed on no verdict); requirement (b): the most-recent non-stale
    verdict is a HOLD not superseded by a later merge_safe.
    """
    head = head_sha.strip().lower()
    state = ReviewVerdict.objects.effective_state_at(slug=slug, pr_id=pr_id, head_sha=head)
    if state is HeadVerdictState.NO_MERGE_SAFE:
        msg = (
            f"no recorded merge_safe ReviewVerdict at the live head {head} for {slug}#{pr_id} — "
            f"refusing to merge (#2829). A merge requires an INDEPENDENT cold-review recorded "
            f"against the exact reviewed tree (`t3 <overlay> ticket clear …` records it as a "
            f"by-product, or `t3 <overlay> review record … --verdict merge_safe`). A force-push "
            f"moves the head and staleness invalidates every prior verdict, so re-record at the "
            f"new head."
        )
        raise MergePreconditionError(msg)
    if state is HeadVerdictState.HOLD:
        msg = (
            f"an independent reviewer recorded a HOLD at this head ({head}) for {slug}#{pr_id} "
            f"not superseded by a later merge_safe — refusing to merge (#2829). The newest "
            f"non-stale verdict at the head is a HOLD; record a later merge_safe to override it."
        )
        raise MergePreconditionError(msg)


def assert_no_active_review_lock(*, slug: str, pr_id: int) -> None:
    """Refuse the merge while a :class:`MRReviewLock` is actively held for the PR (#1405).

    Sibling of :func:`assert_review_verdict_gate` at the same chokepoint
    (:func:`teatree.core.merge.execution.execute_bound_merge`): a recorded
    ``merge_safe`` verdict at the live head is not enough on its own when a
    review is concurrently in flight (``review_dispatched`` /
    ``verdict_pending``, not yet stale) for the SAME MR — that in-flight
    review could still be about to record a HOLD, and a merge racing ahead of
    it would land before the hold ever lands. No row, an ``idle``/``resolved``
    row, or a stale (deadline-passed) row all mean "no review in flight" and
    the merge proceeds.
    """
    lock = MRReviewLock.active_lock_for(slug=slug, pr_id=pr_id)
    if lock is None:
        return
    msg = (
        f"a review is in flight for {slug}#{pr_id} — MRReviewLock state={lock.state!r} "
        f"holder={lock.holder!r} — refusing to merge until the lock resolves (#1405). "
        f"The lock clears when the in-flight review records its verdict, or expires on "
        f"its own once its dispatch deadline passes."
    )
    raise MergePreconditionError(msg)


def assert_merge_provenance_trusted(*, slug: str, pr_id: int, host_kind: str = "github") -> None:
    """Refuse the merge unless the PR head-branch provenance is trusted (#3244).

    The authoritative, load-bearing provenance gate (BLUEPRINT §17.4.3 step 6 /
    invariant 8): every sanctioned merge funnels through ``merge_ticket_pr`` and
    the shared ``execute_bound_merge`` chokepoint, so even a future scanner that
    forgets provenance still cannot auto-merge a fork PR. The overlay merge-guard
    sits in FRONT of this keystone, so relaxing an overlay over-block can never
    relax this gate.

    STRICT fork model — a FORK / cross-repo head branch ALWAYS requires human
    approval, even from a trusted author; a same-repo head is trusted; unreported
    provenance falls back to the identity+visibility author check (fail-closed on
    an untrusted / unknown / empty / unfetchable author on a public repo). The
    same-repo + author reads resolve through :class:`CodeHostQuery`, so GitLab
    overlay MRs cross this gate identically.
    """
    query = CodeHostQuery.for_ref(PrRef(slug=slug, pr_id=pr_id, host_kind=host_kind))
    classification = classify_pr_provenance(
        slug,
        query.pr_author(),
        same_repo=query.pr_same_repo(),
        host_kind=host_kind,
    )
    if classification.internal_repo or classification.trusted:
        return
    msg = (
        f"{slug}#{pr_id} is not trusted to auto-merge — a fork / cross-repo PR always requires a human, "
        f"and an untrusted author on a public repo is refused (§17.4.3 provenance gate / #3244). Merge it "
        f"by hand after an adversarial review, or (same-repo trusted-author fallback) add the handle via "
        f"`t3 identities add <platform> <handle>` if it is genuinely the user."
    )
    raise MergePreconditionError(msg)
