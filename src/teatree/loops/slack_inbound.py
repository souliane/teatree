"""Inbound Slack DMs: record, wake the answer cycle, and the listener-downtime catch-up wiring.

The composition root the receiver (``t3 slack listen``) and the inbox mini-loop share.
The receiver's ``backends`` layer cannot reach this orchestration layer, so both
callables it needs — the per-event ``on_event`` and the hello ``on_connect`` — are
injected from here.
"""

import logging

from teatree.backends.slack.receiver import DeliveredEvents
from teatree.backends.slack.self_identity import is_on_behalf_posted
from teatree.core.backend_factory import messaging_from_overlay
from teatree.loop.scanners.slack_dm_recorder import SlackDmRecorder
from teatree.loops.slack_catchup_job import CatchUpState, SlackCatchUpJob, run_catch_up
from teatree.loops.timer_reconciler import wake_slack_answer

logger = logging.getLogger(__name__)

#: Memoised per overlay for the process's lifetime, so a DM does not re-probe the bot's identity.
_dm_recorders: dict[str, SlackDmRecorder] = {}


def _dm_recorder(overlay: str) -> SlackDmRecorder | None:
    """The recorder for *overlay*; an unresolvable backend is NOT memoised, so it is re-probed next time."""
    recorder = _dm_recorders.get(overlay)
    if recorder is not None:
        return recorder
    backend = messaging_from_overlay(overlay or None)
    if backend is None:
        return None
    recorder = SlackDmRecorder(backend=backend, overlay=overlay)
    _dm_recorders[overlay] = recorder
    return recorder


def reset_dm_recorders() -> None:
    """Drop the per-overlay recorders. Test-only: a recorder outlives ``reset_backend_caches``."""
    _dm_recorders.clear()


def _is_dm(event: dict) -> bool:
    return event.get("type") == "message" and event.get("channel_type") == "im"


def _is_from_someone_else(overlay: str, event: dict) -> bool:
    recorder = _dm_recorder(overlay)
    return recorder is not None and not recorder.is_from_owner(event)


def record_slack_dm(overlay: str, event: dict) -> bool:
    """Record *event* when it is a DM; ``True`` only when a NEW row was written."""
    if not _is_dm(event):
        return False
    recorder = _dm_recorder(overlay)
    if recorder is None:
        logger.warning("No slack backend for overlay %r — the inbox drain will record this DM", overlay)
        return False
    # The durable queue drain owns ignored-event diagnostics after this live attempt.
    return recorder.record_events([event], log_ignored=False) > 0


def record_and_wake_slack_answer(overlay: str, event: dict) -> None:
    """Record an inbound DM, then enqueue an immediate Slack-answer wake.

    Recording FIRST is what makes the wake worth firing: the answer cycle reads
    ``PendingChatInjection.loop_unreplied()``, so a wake that lands before the row
    finds an empty queue and stops without re-arming. An event of ANY type identified as coming from someone
    other than the owner, or from nobody when no owner id is configured, records no row and wakes nothing.
    Nothing is posted to Slack here.
    """
    if _is_from_someone_else(overlay, event):
        logger.info("Ignored non-owner or unverified Slack event for overlay %s (type=%s)", overlay, event.get("type"))
        return
    recorded = record_slack_dm(overlay, event)
    if event.get("channel_type") == "im" and is_on_behalf_posted(event) and not recorded:
        return
    wake_slack_answer.enqueue()


def catch_up_on_connect(overlay: str, alive_at: float, seen: DeliveredEvents | None = None) -> None:
    """The receiver's hello hook; raises when the pass is refused, so the serve loop retries it."""
    state = CatchUpState.load(overlay)
    seen = seen or DeliveredEvents()
    seen.update(state.seen.as_mapping())
    for _ in run_catch_up(
        overlay,
        alive_at=max(alive_at, state.caught_up_at) if state.caught_up_at is not None else alive_at,
        seen=seen,
        record_dm=record_slack_dm,
        wake=wake_slack_answer.enqueue,
    ):
        CatchUpState.save_memo(overlay, seen.as_mapping())
    seen.on_delivery(lambda values: CatchUpState.save_memo(overlay, values))


def slack_catch_up_job(overlay: str) -> SlackCatchUpJob:
    return SlackCatchUpJob(overlay=overlay, record=record_slack_dm, wake=wake_slack_answer.enqueue)
