"""Persist agent-kind dispatch actions as Ticket + Task DB rows.

The statusline is for *displaying*; the DB is for *orchestrating*. When
the tick produces a ``DispatchAction(kind="agent", …)`` — a reviewer
request, an auto-start orchestrator, etc. — this module translates the
action into the appropriate ``Ticket`` and initial ``Task`` rows. The
``/loop`` slot then reads pending Tasks via the loop CLI and spawns
sub-agents in-session.

Idempotency lives here, not at the scanner layer: scanners may emit on
every tick (the ``ReviewerPrsScanner`` cache only updates when the
review task actually completes), but a duplicate enqueue is a no-op
because we look up the existing Ticket+Task before creating new rows.
"""

import logging

from teatree.core.models import Task, Ticket
from teatree.loop.dispatch import DispatchAction

logger = logging.getLogger(__name__)

_OPEN_TASK_STATUSES: frozenset[str] = frozenset({Task.Status.PENDING, Task.Status.CLAIMED})


def persist_agent_actions(actions: list[DispatchAction]) -> list[Task]:
    """Translate ``kind="agent"`` actions into DB rows; return the newly created Tasks.

    Each action is dispatched by ``zone`` to a per-zone handler. Unknown
    zones are logged and skipped — the caller (tick) treats this as
    advisory, not fatal.
    """
    created: list[Task] = []
    for action in actions:
        if action.kind != "agent":
            continue
        handler = _ZONE_HANDLERS.get(action.zone)
        if handler is None:
            logger.debug("No persistence handler for agent zone %r", action.zone)
            continue
        task = handler(action)
        if task is not None:
            created.append(task)
    return created


def _owning_overlay(url: str, scan_tag: str) -> str:
    """Resolve the overlay that *owns* ``url``, preferring URL inference.

    The dispatch payload carries ``overlay`` = the *scanning* overlay's tag
    (``loop/tick.py`` injects ``job.overlay``), not necessarily the overlay
    whose workspace repos own the URL.  When a multi-overlay tick's
    reviewer/orchestrator scanner surfaces an issue/PR owned by a *different*
    overlay, persisting the scan tag leaks the ticket into the scanning
    overlay's statusline zone — and because ``overlay`` is then non-empty,
    ``Ticket.save()`` never runs ``_infer_overlay()`` to correct it (#806,
    incomplete #743).

    Inference (``infer_overlay_for_url``) is the single source of truth; the
    scan tag is only a fallback for the inconclusive case (URL owned by no
    registered overlay, e.g. a host neither overlay declares).
    """
    from teatree.core.overlay_loader import infer_overlay_for_url  # noqa: PLC0415

    return infer_overlay_for_url(url) or scan_tag


def _reconcile_existing_overlay(ticket: Ticket, *, created: bool) -> None:
    """Correct an already-persisted ticket whose overlay no longer matches.

    ``get_or_create`` may resolve a pre-existing row whose ``overlay`` was
    written from a stale/wrong scan tag.  Re-infer from ``issue_url`` and
    persist the correction.  ``apply_inferred_overlay`` keeps the #743
    invariant: an inconclusive (empty) inference never blanks a value that
    is already set, so a host no overlay declares is left as-is.
    """
    if created:
        return
    ticket.reconcile_overlay()


def _handle_reviewer(action: DispatchAction) -> Task | None:
    """Reviewer-requested PR → Ticket(role=reviewer) + Task(phase=reviewing)."""
    payload = action.payload
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
        ticket.merge_extra(set_keys={"reviewed_sha": head_sha}, pop_keys=["last_review_state"])
    if _has_open_task(ticket, phase="reviewing"):
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
        logger.debug("PR %s already approved at head %s — not re-enqueuing review", pr_url, head_sha)
        return None
    from teatree.core.models.ticket import schedule_external_review  # noqa: PLC0415

    return schedule_external_review(ticket)


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
    from teatree.core.backend_protocols import ReviewState  # noqa: PLC0415

    extra = ticket.extra or {}
    terminal = {ReviewState.APPROVED.value, ReviewState.REVIEWED_NO_ACTION.value}
    return extra.get("last_review_state") in terminal and extra.get("reviewed_sha") == head_sha


def _handle_orchestrator(action: DispatchAction) -> Task | None:
    """Auto-start assigned issue → Ticket(role=author) + Task(phase=coding).

    Only fires for ``assigned_issue.ready`` / ``issue_implementer.claimed``
    signals that carry ``auto_start=True`` (the dispatcher already filtered).
    The scheduled coding task is then dispatched per-phase by the loop —
    ``pending_task`` signals route directly to the phase's own agent, not
    through ``t3:orchestrator``.
    """
    payload = action.payload
    if payload.get("auto_start") is not True:
        return None
    issue_url = str(payload.get("issue_url") or payload.get("url") or "")
    if not issue_url:
        logger.debug("Skipping t3:orchestrator action with no issue_url: %r", action.detail)
        return None
    scan_tag = str(payload.get("overlay") or "")
    ticket, created = Ticket.objects.get_or_create(
        issue_url=issue_url,
        defaults={"overlay": _owning_overlay(issue_url, scan_tag), "role": Ticket.Role.AUTHOR},
    )
    _reconcile_existing_overlay(ticket, created=created)
    # #748: a loop/coordinator-built ticket must have a durable phase-
    # attestation session even when scheduling below is skipped (role
    # mismatch / not NOT_STARTED / open task), so the shipping gate can
    # reconcile real work instead of fail-closing on "no session".
    ticket.ensure_session()
    if ticket.role != Ticket.Role.AUTHOR:
        logger.debug(
            "Ticket %s for %s has role=%s; not scheduling coding",
            ticket.pk,
            issue_url,
            ticket.role,
        )
        return None
    if _has_open_task(ticket, phase="coding") or ticket.state != Ticket.State.NOT_STARTED:
        return None
    return ticket.schedule_coding()


def _has_open_task(ticket: Ticket, *, phase: str) -> bool:
    # #769 audit: match any accepted phase spelling (short verb or
    # gerund) via the shared SSOT helper, not a raw ``phase=phase``
    # filter that would miss a short-verb ``code`` task and let the
    # orchestrator create a duplicate.
    return Task.objects.pending_in_phase(phase).filter(ticket=ticket).exists()


_ZONE_HANDLERS = {
    "t3:reviewer": _handle_reviewer,
    "t3:orchestrator": _handle_orchestrator,
}


__all__ = ["persist_agent_actions"]
