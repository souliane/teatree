import logging

from django.utils import timezone

from teatree.core.models.incoming_event import IncomingEvent
from teatree.loop.dispatch import dispatch
from teatree.loop.persistence import persist_agent_actions
from teatree.loop.scanners import event_handlers
from teatree.loop.scanners.event_handlers import HandlingOutcome
from teatree.loops.enable_verdict import loop_admits

logger = logging.getLogger(__name__)


def handle_incoming_event(event: IncomingEvent) -> HandlingOutcome:
    handler = event_handlers.HANDLERS.lookup(event.source, event.event_name, event.action)
    if handler is None:
        return HandlingOutcome.SKIPPED
    if not loop_admits(handler.loop):
        return HandlingOutcome.DEFERRED
    if not IncomingEvent.objects.claim_for_handling(event.pk, timezone.now()):
        return HandlingOutcome.SKIPPED
    event.refresh_from_db()
    errors: dict[str, str] = {}
    try:
        persist_agent_actions(dispatch(handler.handle(event), errors=errors), errors=errors)
    except Exception as exc:
        logger.exception("Handler for incoming event %s raised", event.pk)
        return _retry_or_dead_letter(event, f"{type(exc).__name__}: {exc}")
    if errors:
        return _retry_or_dead_letter(event, "; ".join(f"{key}: {detail}" for key, detail in sorted(errors.items())))
    event.mark_processed()
    return HandlingOutcome.PROCESSED


def _retry_or_dead_letter(event: IncomingEvent, error: str) -> HandlingOutcome:
    return HandlingOutcome.DEAD_LETTERED if event.record_failure(error) else HandlingOutcome.RETRY
