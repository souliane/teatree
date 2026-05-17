"""The missing ``t3`` merge FSM transition — loop-executes side (BLUEPRINT §17.4).

This is the keystone the factory was missing: the only sanctioned path from
``IN_REVIEW`` → ``MERGED``. Raw ``gh pr merge`` / ``glab mr merge`` bypasses the
ledger update, the HEAD/workstream attestation binding, the privacy/AI-signature
scan, and ``mark_merged()`` — leaving the FSM incoherent. The prohibition guard
(``hook_router._BLOCKED_COMMANDS``) mechanically refuses the raw path; this
module is the coherent replacement.

Flow (orchestrator-decides / loop-executes, §17.4.1):

Pre-condition hook — ``assert_merge_preconditions`` runs the loop's §17.4.3
validation in order: a valid, actionable ``MergeClear`` row re-read from the
DB; CI green on the exact PR head; an independent cold-review CLEAR recorded
(a ``reviewer_identity`` distinct from the executing loop — §17.8 clause 3);
plus the §17.4.3 SHA-match and not-draft checks. ``substrate`` blast-class PRs
are never auto-merged here (invariant 4 / §17.4.3 step 5).

Atomic merge — ``execute_bound_merge`` binds the merge to
``expected_head_oid`` so a force-push landing in the TOCTOU window is rejected
by GitHub and treated as a failed check, never a retry-with-new-head (the
E10-class staleness/replay defence).

Post hook — ``record_merge_and_advance`` runs in one ``transaction.atomic()``:
consume the CLEAR, write the ``MergeAudit`` row, bind the phase attestation to
the merged HEAD, and call ``ticket.mark_merged()``. State-change and the
durable merge record land atomically (the §4 worker-enqueue / sync-atomicity
invariant).
"""

import json
import logging
import shutil
from dataclasses import dataclass
from typing import TypedDict, cast

from django.apps import apps
from django.db import transaction
from django.utils import timezone

from teatree.utils.run import run_allowed_to_fail

logger = logging.getLogger(__name__)


class MergePreconditionError(RuntimeError):
    """A §17.4.3 pre-condition check failed — the loop must not merge.

    The caller re-escalates into the durable backlog (it never self-issues a
    replacement CLEAR) and leaves the FSM unchanged.
    """


class MergeHeadMovedError(MergePreconditionError):
    """GitHub rejected the merge because the head moved off ``expected_head_oid``.

    Treated as a failed check, NOT a retry-with-new-head (§17.4.3): the loop
    never re-resolves the head and proceeds.
    """


class MergeReplayError(MergePreconditionError):
    """The CLEAR was already consumed when re-checked UNDER the row lock.

    ``assert_merge_preconditions`` reads ``is_actionable()`` without the row
    lock; two executors that both pass that unlocked check would otherwise
    both reach the post hook and double-consume the single-use CLEAR (a
    double ``MergeAudit`` / double ``mark_merged()``). The post hook re-reads
    the row ``select_for_update``-locked and re-asserts ``consumed_at is
    None`` so exactly one executor wins; the loser raises this.
    """


@dataclass(frozen=True, slots=True)
class MergeOutcome:
    pr_id: int
    slug: str
    merged_sha: str
    ticket_state: str


def _run_gh(argv: list[str]) -> tuple[int, str, str]:
    gh = shutil.which("gh") or "gh"
    result = run_allowed_to_fail([gh, *argv], expected_codes=None)
    return result.returncode, result.stdout, result.stderr


def fetch_live_head_sha(slug: str, pr_id: int) -> str:
    """The PR's current head SHA from GitHub (never a branch ref) — §17.4.3 step 2."""
    rc, out, _ = _run_gh(
        ["pr", "view", str(pr_id), "--repo", slug, "--json", "headRefOid", "--jq", ".headRefOid"],
    )
    return out.strip() if rc == 0 else ""


def fetch_pr_is_draft(slug: str, pr_id: int) -> bool:
    """Whether the PR is in draft state — §17.4.3 step 4."""
    rc, out, _ = _run_gh(
        ["pr", "view", str(pr_id), "--repo", slug, "--json", "isDraft", "--jq", ".isDraft"],
    )
    return rc == 0 and out.strip().lower() == "true"


class _RollupEntry(TypedDict, total=False):
    """One ``gh ... statusCheckRollup`` entry — CheckRun or StatusContext."""

    conclusion: object
    status: object
    state: object


def _classify_check(check: object) -> str:
    """Map one rollup entry to ``green`` / ``pending`` / ``failed``.

    CheckRun entries use ``conclusion`` + ``status``; legacy StatusContext
    entries use ``state``. A non-dict entry is ignored by the caller.
    """
    if not isinstance(check, dict):
        return ""
    entry = cast("_RollupEntry", check)
    conclusion = str(entry.get("conclusion") or "").upper()
    status = str(entry.get("status") or "").upper()
    state = str(entry.get("state") or "").upper()
    if status and status != "COMPLETED":
        return "pending"
    if conclusion in {"SUCCESS", "NEUTRAL", "SKIPPED"} or state == "SUCCESS":
        return "green"
    if state == "PENDING":
        return "pending"
    return "failed"


def _rollup_verdict(statuses: list[str]) -> str:
    if "failed" in statuses:
        return "failed"
    if "pending" in statuses:
        return "pending"
    return "green"


def fetch_required_checks_status(slug: str, pr_id: int) -> str:
    """Live required-checks rollup for the PR head — §17.4.3 step 3.

    Evaluated against GitHub's live rollup at merge time (the authoritative
    set), NOT the ``gh_verify_result`` snapshot saved on the CLEAR. Returns
    ``"green"`` only when every reported check concluded successfully;
    ``"pending"`` while any is still running; otherwise the failing state.
    """
    rc, out, _ = _run_gh(
        [
            "pr",
            "view",
            str(pr_id),
            "--repo",
            slug,
            "--json",
            "statusCheckRollup",
            "--jq",
            ".statusCheckRollup",
        ],
    )
    if rc != 0:
        return "failed"
    try:
        rollup = json.loads(out) if out.strip() else []
    except json.JSONDecodeError:
        return "failed"
    if not isinstance(rollup, list):
        return "failed"
    statuses = [verdict for check in rollup if (verdict := _classify_check(check))]
    return _rollup_verdict(statuses) if statuses else "green"


def assert_merge_preconditions(
    *,
    clear: object,
    executing_loop_identity: str,
    slug: str,
    pr_id: int,
    human_authorized: str = "",
) -> str:
    """Run the §17.4.3 loop validation in order; return the verified head SHA.

    Raises :class:`MergePreconditionError` on the first failed check. The
    durable-backlog re-escalation is the caller's responsibility (§17.4.3) —
    this function never self-issues a replacement CLEAR.

    ``human_authorized`` is the only escape from the substrate auto-merge
    refusal (step 5). It is empty for every loop-driven merge, so the loop
    still never auto-merges substrate. A non-empty value unlocks the merge
    **only** when the CLEAR is substrate-class AND its recorded
    ``human_authorizer`` matches: the substrate change requires a recorded
    human authorisation, and on re-presentation **the agent executes** the
    merge through this same sanctioned ``t3`` transition (invariant 8: even an
    owner-approved merge goes through this transition, never raw ``gh`` and
    never a human-performed merge action — approval is the gate, execution is
    always the agent). It can never unlock a non-substrate CLEAR, so it cannot
    be used to bypass independent loop review of logic/docs PRs.
    """
    from teatree.core.models import MergeClear  # noqa: PLC0415

    if not isinstance(clear, MergeClear):
        msg = f"no MergeClear row for {slug}#{pr_id} — refusing to merge (§17.4.3 step 1)"
        raise MergePreconditionError(msg)

    # 1. CLEAR exists, all fields populated, unconsumed.
    if not clear.is_actionable():
        msg = (
            f"MergeClear for {slug}#{pr_id} is not actionable (missing fields or already "
            f"consumed) — treated as absent (§17.4.2/§17.4.3 step 1)"
        )
        raise MergePreconditionError(msg)

    # Independent cold-review CLEAR: the reviewer identity must be distinct
    # from the executing loop (§17.8 clause 3 — the loop cannot rubber-stamp
    # its own CLEAR).
    if clear.reviewer_identity.strip() == executing_loop_identity.strip():
        msg = (
            f"MergeClear reviewer_identity ({clear.reviewer_identity!r}) equals the "
            f"executing loop identity — a CLEAR must be issued by an independent "
            f"cold reviewer, not self-issued (§17.8 clause 3)"
        )
        raise MergePreconditionError(msg)

    # The human-substrate escape is substrate-only. Presenting it against a
    # non-substrate CLEAR is refused outright so the path can never be used to
    # short-circuit independent loop review of a logic/docs PR (the loop is
    # the reviewer-of-record for those — invariant 8 / §17.4.1).
    presented = human_authorized.strip()
    if presented and not clear.is_substrate():
        msg = (
            f"--human-authorized presented for non-substrate MergeClear "
            f"({slug}#{pr_id}, blast_class={clear.blast_class}); the recorded-human-"
            f"approval path is substrate-only — a logic/docs CLEAR merges through "
            f"the loop, not via a human-approval escape hatch (invariant 8 / §17.4.1)"
        )
        raise MergePreconditionError(msg)

    # 5. blast_class respected — the loop NEVER auto-merges substrate-class
    #    PRs regardless of CLEAR validity (invariant 4 / §17.4.3 step 5). The
    #    ONLY exception: a substrate CLEAR whose recorded ``human_authorizer``
    #    matches the value re-presented at merge time. The recorded human
    #    approval is the gate; the AGENT then executes through this same
    #    SHA-bound, audited transition (invariant 8) — not raw ``gh``, not a
    #    human-performed merge. The approval is recorded durably on the CLEAR
    #    and bound to the merge.
    if clear.is_substrate() and not clear.human_merge_authorized_by(presented):
        detail = (
            "no human authoriser recorded on the CLEAR"
            if not clear.human_authorizer
            else f"presented authoriser != recorded ({clear.human_authorizer!r})"
            if presented
            else "no --human-authorized presented at merge time"
        )
        msg = (
            f"MergeClear for {slug}#{pr_id} is blast_class=substrate — substrate "
            f"changes require a recorded human approval and are draft-locked "
            f"(invariant 4); the loop never auto-merges them (§17.4.3 step 5). "
            f"{detail.capitalize()}. The sanctioned path: an owner issues `t3 "
            f"<overlay> ticket clear … --blast-class substrate --human-authorize "
            f"<id>` (the recorded approval — the gate), then the agent executes "
            f"`t3 <overlay> ticket merge <clear_id> --human-authorized <id>`"
        )
        raise MergePreconditionError(msg)

    # 2. SHA still matches — re-fetch the live head; it must equal reviewed_sha.
    live_sha = fetch_live_head_sha(slug, pr_id)
    if not live_sha:
        msg = f"could not resolve the live head SHA for {slug}#{pr_id} (§17.4.3 step 2)"
        raise MergePreconditionError(msg)
    if live_sha != clear.reviewed_sha:
        msg = (
            f"PR head moved: live={live_sha[:8]} != reviewed={clear.reviewed_sha[:8]} — "
            f"the CLEAR is stale (force-push / new commits). Re-escalate; the loop never "
            f"self-issues a replacement (§17.4.3 step 2)"
        )
        raise MergePreconditionError(msg)

    # 4. Not draft.
    if fetch_pr_is_draft(slug, pr_id):
        msg = f"{slug}#{pr_id} is in draft state — refusing to merge (§17.4.3 step 4)"
        raise MergePreconditionError(msg)

    # 3. CI still green — against GitHub's LIVE rollup, not the saved snapshot.
    checks = fetch_required_checks_status(slug, pr_id)
    if checks != "green":
        msg = (
            f"live required-checks for {slug}#{pr_id} are {checks!r}, not green — "
            f"refusing to merge (§17.4.3 step 3; the live list is the source of "
            f"truth, not the CLEAR snapshot)"
        )
        raise MergePreconditionError(msg)

    return live_sha


def execute_bound_merge(*, slug: str, pr_id: int, expected_head_oid: str) -> str:
    """Squash-merge bound to ``expected_head_oid`` — fail closed on head drift.

    Uses the GitHub merge API ``expected_head_oid`` parameter (``PUT
    .../pulls/N/merge``). If GitHub reports the head moved, the merge is
    refused and raised as :class:`MergeHeadMovedError` — a failed check, never
    a retry-with-new-head (§17.4.3 "bind execution to the exact verified SHA,
    fail closed").
    """
    endpoint = f"repos/{slug}/pulls/{pr_id}/merge"
    rc, out, err = _run_gh(
        [
            "api",
            "--method",
            "PUT",
            endpoint,
            "-f",
            "merge_method=squash",
            "-f",
            f"sha={expected_head_oid}",
        ],
    )
    if rc != 0:
        combined = f"{out}\n{err}".lower()
        if "head" in combined and ("modif" in combined or "changed" in combined or "409" in combined):
            msg = (
                f"GitHub refused the merge of {slug}#{pr_id}: head moved off "
                f"{expected_head_oid[:8]} (expected_head_oid mismatch). Treated as a "
                f"failed check — NOT retried with a new head (§17.4.3)"
            )
            raise MergeHeadMovedError(msg)
        msg = f"merge of {slug}#{pr_id} failed: {err.strip() or out.strip() or 'gh api non-zero'}"
        raise MergePreconditionError(msg)

    try:
        merged = json.loads(out) if out.strip() else {}
    except json.JSONDecodeError:
        merged = {}
    merged_sha = str(merged.get("sha") or "") if isinstance(merged, dict) else ""
    return merged_sha or expected_head_oid


def record_merge_and_advance(
    *,
    clear: object,
    merged_sha: str,
    required_checks_status: str,
) -> str:
    """Post hook: consume CLEAR, write audit, bind attestation, ``mark_merged()``.

    All in ONE ``transaction.atomic()`` so the FSM advance and the durable
    merge record land atomically (the §4 worker-enqueue / sync-atomicity
    invariant — a crash mid-post leaves a re-runnable, not a half-merged,
    state). Returns the resulting ticket state.
    """
    from teatree.core.models import MergeClear  # noqa: PLC0415

    if not isinstance(clear, MergeClear):  # pragma: no cover - guarded by caller
        msg = "record_merge_and_advance requires a MergeClear instance"
        raise MergePreconditionError(msg)

    merge_audit_model = apps.get_model("core", "MergeAudit")
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
        )
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
        if ticket.state in {"in_review", "merged"}:
            ticket.mark_merged()
            ticket.save()
        return ticket.state


def merge_ticket_pr(
    *,
    clear: object,
    executing_loop_identity: str,
    human_authorized: str = "",
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
    """
    from teatree.core.models import MergeClear  # noqa: PLC0415

    if not isinstance(clear, MergeClear):
        msg = "merge_ticket_pr requires a MergeClear instance"
        raise MergePreconditionError(msg)

    slug = clear.slug
    pr_id = clear.pr_id
    verified_sha = assert_merge_preconditions(
        clear=clear,
        executing_loop_identity=executing_loop_identity,
        slug=slug,
        pr_id=pr_id,
        human_authorized=human_authorized,
    )
    merged_sha = execute_bound_merge(slug=slug, pr_id=pr_id, expected_head_oid=verified_sha)
    checks = fetch_required_checks_status(slug, pr_id)
    state = record_merge_and_advance(
        clear=clear,
        merged_sha=merged_sha,
        required_checks_status=checks,
    )
    logger.info(
        "merge keystone: %s#%s merged at %s; ticket state=%s",
        slug,
        pr_id,
        merged_sha[:8],
        state or "(no ticket)",
    )
    return MergeOutcome(pr_id=pr_id, slug=slug, merged_sha=merged_sha, ticket_state=state)
