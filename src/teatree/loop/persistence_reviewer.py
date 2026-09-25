"""Persistence handler for the reviewer-requested-PR dispatch zone.

Split out of :mod:`teatree.loop.persistence` to keep that hub under the module-health LOC
cap, and cohesive on its own terms: everything here decides whether a reviewing task is
owed for a pull request, and mints it when it is.

Three rungs say not to mint — one is already open, this head was already reviewed, or no
verdict this review produced could be recorded at all. The last is the expensive one:
until it existed the producers re-emitted every tick into a seam with no failure memory,
so a pull request carrying no recorded head minted a brand-new task per tick, each paying
for a full review whose verdict was then discarded.

Imports the hub's shared ticket helpers at top level; the hub imports this module only
lazily, the same load-bearing edge :mod:`teatree.loop.persistence_self_pr_review` relies
on to break the cycle.
"""

import logging

from teatree.core.gates.review_recordability_gate import unrecordable_review_mint_refusal
from teatree.core.modelkit.review_state import DISCHARGED_REVIEW_STATES
from teatree.core.models import DeferredQuestion, Task, Ticket
from teatree.core.models.ticket_external_review import schedule_external_review
from teatree.loop.dispatch import DispatchAction
from teatree.loop.dispatch_tables import ActionPayload
from teatree.loop.persistence import _owning_overlay, _reconcile_existing_overlay
from teatree.loop.persistence_phase_task import open_task_in_phase

logger = logging.getLogger(__name__)


def handle_reviewer(action: DispatchAction) -> Task | None:
    """Reviewer-requested PR → Ticket(role=reviewer) + Task(phase=reviewing).

    The ``self_pr`` payload flag (set by the #3569 Claude self-PR scanner) routes
    to :func:`_handle_self_pr_review` — the SAME ``t3:reviewer`` agent + reviewing
    phase, but per-SHA deduped via :class:`CodexReviewMarker` because the self-PR
    scanner emits unconditionally every tick (the colleague path is deduped by the
    reviewer cache instead).
    """
    payload = action.payload
    if payload.get("self_pr"):
        # Lazy: the sibling imports this module's shared ticket/task helpers, so it
        # can only be imported after this module finishes loading (breaks the cycle).
        from teatree.loop.persistence_self_pr_review import handle_self_pr_review  # noqa: PLC0415 — #3569

        return handle_self_pr_review(action)
    pr_url = str(payload.get("url") or "")
    if not pr_url:
        logger.debug("Skipping t3:reviewer action with no url: %r", action.detail)
        return None
    head_sha = str(payload.get("head_sha") or "")
    scan_tag = str(payload.get("overlay") or "")
    ticket, created = Ticket.objects.get_or_create(
        issue_url=pr_url,
        defaults={
            "overlay": _owning_overlay(pr_url, scan_tag),
            "role": Ticket.Role.REVIEWER,
            "extra": {"reviewed_sha": head_sha} if head_sha else {},
        },
    )
    _reconcile_existing_overlay(ticket, created=created)
    if ticket.role != Ticket.Role.REVIEWER:
        logger.debug(
            "Ticket %s exists with role=%s, not promoting to reviewer for PR %s",
            ticket.pk,
            ticket.role,
            pr_url,
        )
        return None
    if head_sha and (ticket.extra or {}).get("reviewed_sha") != head_sha:
        # #800 N3: canonical locked RMW — a concurrent pr_urls /
        # visual_qa writer no longer clobbers reviewed_sha.
        #
        # #959 defect 2: a SHA move invalidates any prior approval — drop
        # ``last_review_state`` in the same RMW so the
        # ``_already_reviewed_at_head`` dedup below does NOT suppress
        # review of the genuinely new revision (the recorded APPROVED
        # belonged to the old SHA).
        ticket.merge_extra(set_keys={"reviewed_sha": head_sha}, pop_keys=["last_review_state", "discharged_sha"])
    if payload.get("invalidates_discharge"):
        ticket.merge_extra(pop_keys=["discharged_sha"])
    return _mint_reviewer_task(ticket, payload=payload, head_sha=head_sha)


def _mint_reviewer_task(ticket: Ticket, *, payload: ActionPayload, head_sha: str) -> Task | None:
    """Schedule *ticket*'s reviewing task, or ``None`` when a rung says not to.

    Three rungs, each a reason NOT to mint: one is already open, one already reviewed this
    head, or no verdict this review produced could be recorded at all.
    """
    open_task = open_task_in_phase(ticket, phase="reviewing")
    if open_task is not None:
        _link_broadcast_reviewer_task(payload, open_task)
        return None
    if _already_reviewed_at_head(ticket, head_sha):
        # #959 defect 2: the MR was already independently reviewed AND
        # approved (e.g. an out-of-band review pass) at the CURRENT head
        # SHA. There is no *open* reviewing Task — the prior dedup
        # (open-task-only) re-enqueued review every tick (the live tasks
        # 49/50/51 for the already-approved SSO-mock MRs). A recorded
        # forge approval matching the current head is authoritative
        # "already reviewed"; a SHA move resets it via the mismatch path
        # above, so a genuinely new revision is still reviewed.
        logger.debug("PR %s already approved at head %s — not re-enqueuing review", ticket.issue_url, head_sha)
        return None
    unrecordable = unrecordable_review_mint_refusal(ticket)
    if unrecordable is not None:
        _escalate_unrecordable_review(ticket, reason=unrecordable)
        return None
    task = schedule_external_review(ticket)
    _link_broadcast_reviewer_task(payload, task)
    return task


def _escalate_unrecordable_review(ticket: Ticket, *, reason: str) -> None:
    """Name a review nobody can record, once per ticket, and mint no task for it.

    The producers re-emit every tick and this seam has no failure memory, so without the
    refusal above each tick minted a fresh task that paid for a full review and discarded
    its verdict. Refusing silently would trade that burn for an invisible gap, so the
    condition is recorded where the operator already looks — deduped per ticket across
    ANSWERED questions too, exactly like ``stuck_ticket_redispatch._escalate_once``, so
    answering it never resurrects a fresh one.

    Self-healing rather than latched: the rung re-evaluates every tick, so the first
    emission carrying a real head stamps ``reviewed_sha`` above and mints normally.
    """
    marker = f"review-unrecordable:{ticket.pk}"
    logger.warning("Not minting a review for ticket %s: %s", ticket.pk, reason)
    if DeferredQuestion.objects.filter(dedupe_marker=marker).exists():
        return
    DeferredQuestion.record(
        f"{reason} Nothing is scheduled for it. Should the head be supplied, or the reviewer ticket closed?",
        session_id="",
        dedupe_marker=marker,
        audience=DeferredQuestion.Audience.INTERNAL,
    )


def _link_broadcast_reviewer_task(payload: ActionPayload, task: Task | None) -> None:
    """Record the covering reviewer task on the ``ScannedBroadcast`` row that emitted it.

    Closes the broadcast ledger's emission gate: until the row knows which task
    covers it, ``ScannedBroadcast.awaiting_reviewer_dispatch`` keeps re-emitting
    the review intent every tick. Best-effort — a broadcast that has since been
    deleted must never break the dispatch it is auditing.
    """
    broadcast_id = payload.get("broadcast_id")
    if not broadcast_id or task is None:
        return
    from teatree.core.models import ScannedBroadcast  # noqa: PLC0415 — lazy: avoids the models import cycle

    row = ScannedBroadcast.objects.filter(pk=broadcast_id).first()
    if row is not None:
        row.attach_reviewer_task(str(task.pk))


def _already_reviewed_at_head(ticket: Ticket, head_sha: str) -> bool:
    """Has this PR a recorded terminal review observation at the current head?

    The dedup signal for an *out-of-band* review (one not driven by a
    loop reviewing Task, so there is no open/completed Task to key on) is
    the reviewer ticket's ``last_review_state``/``reviewed_sha`` pair —
    written by ``Ticket.mark_reviewed_externally`` / ``mark_review_no_action``
    and the ``ReviewerPrsScanner`` cache. A terminal state at the current
    head ⇒ the review already happened; re-enqueueing would duplicate it
    every tick. Two terminal states suppress: ``APPROVED`` (a genuine
    approving review — the existing #959 behaviour) and
    ``REVIEWED_NO_ACTION`` (the reviewer concluded there was nothing to
    post/approve on a bot MR — before #1077 there was no terminal state
    for this, so the reviewing task re-dispatched every Stop-hook pump
    forever). ``REVIEWED_NO_ACTION`` is intentionally *not* APPROVED so a
    future genuine review is never hidden; suppression is keyed on the
    head SHA, and a SHA move drops ``last_review_state`` (the #959 reset
    in ``_handle_reviewer``) so a new revision is still reviewed.

    A blank ``head_sha`` is treated as "cannot confirm parity" so review
    is NOT suppressed (fail-open — never silently skip a real review).
    """
    if not head_sha:
        return False
    extra = ticket.extra or {}
    if extra.get("discharged_sha") == head_sha:
        return True
    return extra.get("last_review_state") in DISCHARGED_REVIEW_STATES and extra.get("reviewed_sha") == head_sha
