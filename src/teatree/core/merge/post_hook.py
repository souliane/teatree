"""The §17.4 keystone post hook: consume the CLEAR, write the audit, advance the FSM.

Split from :mod:`teatree.core.merge.execution` — the precondition/bound-merge concern
ends the moment the forge merge is irreversible, and everything after it is one
atomic DB write. :func:`record_merge_and_advance` is what
:func:`teatree.core.merge.execution.merge_ticket_pr` calls once the merge has landed.
"""

import logging
from dataclasses import dataclass

from django.apps import apps
from django.db import transaction
from django.utils import timezone
from django_fsm import TransitionNotAllowed

from teatree.core.merge.errors import MergePreconditionError, MergeReplayError
from teatree.core.merge.pr_slug_resolution import normalize_repo_slug, resolved_repo_slug
from teatree.core.models import MergeClear

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MergeAuditAuthorizers:
    """The authorizer ids the post hook stamps on the ``MergeAudit`` for a non-default merge.

    Both empty for an ordinary merge. ``expedited_by`` is the PENDING-checks
    expedite waiver authoriser (§17.4.3 / PR-07); ``standing_delegation_by`` is the
    config-sourced standing substrate authorizer (#3413). Grouped so
    :func:`record_merge_and_advance` takes one audit-authorizer argument instead of
    a widening list.
    """

    expedited_by: str = ""
    standing_delegation_by: str = ""


def _supersede_siblings(locked: MergeClear, *, repo_slug: str) -> None:
    """§15: consume every sibling unconsumed CLEAR for the SAME repo's PR.

    A re-review at a moved head issues a fresh CLEAR at the new SHA, leaving the
    older one unconsumed; once THIS merge consumes one, its siblings are no longer a
    stalled merge, so they are consumed in the caller's atomic block under the row
    lock (a single serialized UPDATE) rather than left to ratchet S4 hard-red forever.

    The scope is the REPO, never the stored ``slug`` alone. A ticketless CLEAR's slug
    is a workstream name (``merge-candidate-working-repos``) that several repos share,
    so keying on ``(slug, pr_id)`` let a merge in one repo stamp another repo's live,
    independently-reviewed CLEAR for its own PR #42 as consumed — that CLEAR then
    refuses its merge as "already consumed" and owes a fresh cold review for work
    that never merged. So a workstream-slug sibling is superseded only when it
    resolves to the same repo this merge landed in, and when the merge-time repo is
    unknown (a legacy caller passing no ``repo_slug``) nothing is superseded at all.
    An ``owner/repo``-shaped slug is already repo-scoped and keeps the plain
    case-insensitive match a forge slug's case-insensitivity requires.
    """
    siblings = (
        MergeClear.objects.filter(slug__iexact=locked.slug, pr_id=locked.pr_id, consumed_at__isnull=True)
        .exclude(pk=locked.pk)
        .select_related("ticket")
    )
    if normalize_repo_slug(locked.slug):
        superseded = [sibling.pk for sibling in siblings]
    elif repo_slug:
        superseded = [
            sibling.pk for sibling in siblings if resolved_repo_slug(sibling).casefold() == repo_slug.casefold()
        ]
    else:
        logger.warning(
            "merge keystone: %s#%s carries a workstream slug and no merge-time repo — "
            "skipping the §15 sibling supersede rather than risk consuming another repo's live CLEAR",
            locked.slug,
            locked.pr_id,
        )
        return
    if superseded:
        MergeClear.objects.filter(pk__in=superseded).update(consumed_at=locked.consumed_at)


def record_merge_and_advance(
    *,
    clear: object,
    merged_sha: str,
    required_checks_status: str,
    repo_slug: str = "",
    authorizers: MergeAuditAuthorizers | None = None,
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

    stamps = authorizers or MergeAuditAuthorizers()
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
                expedited_by=stamps.expedited_by,
                repo_slug=repo_slug,
                standing_delegation_by=stamps.standing_delegation_by,
            )
            _supersede_siblings(locked, repo_slug=repo_slug)
            # The forge merge already landed, so the PR row is MERGED and a ticketless
            # CLEAR (``--ticket-id`` is optional, and the loop never passed one) can
            # recover from the PR the FSM it otherwise has nothing to advance.
            ticket = locked.record_merged_pull_request(repo_slug)
            if ticket is None:
                # #3840: the merge landed, the CLEAR is consumed and the audit is written,
                # but no FSM advanced. Returning "" quietly made that indistinguishable from
                # a merge with nothing to advance, so a board that never moved looked healthy.
                # `t3 <overlay> ticket backfill-clears` links the row once the PR is attributable.
                logger.warning("merge keystone: %s#%s merged but resolved no owning ticket", locked.slug, locked.pr_id)
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
