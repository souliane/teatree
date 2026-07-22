"""Persistence handler for the Claude self-PR review dispatch (#3569).

Split out of :mod:`teatree.loop.persistence` to keep that hub under the
module-health LOC cap; semantically it is a sibling of ``_handle_codex_review``.
It is the codex fallback: a self-authored PR routed to the ``t3:reviewer``
sub-agent (a ``reviewing``-phase Task) instead of a codex slash-command agent,
sharing the per-SHA :class:`CodexReviewMarker` idempotency so the unconditional
scanner emit dedups per head SHA instead of flooding the queue. The marker rides
the SAME atomic block that creates the Task — a dropped persist rolls it back and
the next tick retries; a force-push (new head SHA) re-fires the review.

Imports the shared ticket/task helpers from :mod:`teatree.loop.persistence` at
top level; that hub imports this module only lazily (inside ``_handle_reviewer``),
so there is no import cycle.
"""

import logging

from django.db import transaction

from teatree.core.models import Task, Ticket
from teatree.core.models.codex_review_marker import CodexReviewMarker
from teatree.loop.dispatch import DispatchAction
from teatree.loop.persistence import _create_phase_task, _get_or_create_ticket, _has_open_task, _owning_overlay

logger = logging.getLogger(__name__)


def handle_self_pr_review(action: DispatchAction) -> Task | None:
    """Self-authored PR → reviewer ticket + Claude ``reviewing`` task, per-SHA deduped."""
    payload = action.payload
    pr_url = str(payload.get("pr_url") or payload.get("url") or "")
    slug = str(payload.get("slug") or "")
    pr_id = payload.get("pr_id")
    head_sha = str(payload.get("head_sha") or "")
    if not pr_url or not slug or not isinstance(pr_id, int) or not head_sha:
        logger.debug("Skipping self-PR review action with incomplete payload: %r", action.detail)
        return None
    variant = str(payload.get("variant") or "claude:review")
    overlay = str(payload.get("overlay") or "")
    with transaction.atomic():
        ticket, _created = _get_or_create_ticket(
            pr_url,
            role=Ticket.Role.REVIEWER,
            overlay=_owning_overlay(pr_url, overlay),
            extra={"reviewed_sha": head_sha, "self_pr_review_variant": variant},
        )
        if ticket.role != Ticket.Role.REVIEWER or _has_open_task(ticket, phase="reviewing"):
            return None
        marker = CodexReviewMarker.claim(
            slug=slug,
            pr_id=pr_id,
            head_sha=head_sha,
            overlay=overlay,
            variant=variant,
        )
        if marker is None:
            return None
        return _create_phase_task(
            ticket,
            phase="reviewing",
            agent_id="reviewer",
            reason=f"Auto-scheduled self-PR review ({variant}) — {pr_url}",
        )


__all__ = ["handle_self_pr_review"]
