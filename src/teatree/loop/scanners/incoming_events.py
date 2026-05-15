"""Scanner that drains the ``IncomingEvent`` ingestion queue (#669).

Sits at the consumer end of the autonomous-events stack:

1. Reads ``IncomingEvent.objects.unprocessed()`` (limited per tick).
2. Classifies each via :func:`teatree.core.intent_classifier.classify_event`.
3. Routes each via :func:`teatree.core.event_router.route_event`.
4. Executes the side effect for the routed action.
5. Marks the event ``processed_at`` so it does not re-fire.

The first three steps are pure functions and were already covered by
``IntentClassification`` (#665) and the router (#667). This scanner is
the missing piece that actually drives them — without it the receivers
persist rows that sit forever.

The ``schedule_task`` and ``schedule_merge`` actions emit user-facing
``ScanSignal``s rather than auto-creating Tickets — automatic ticket
creation from inbound chat needs a separate decision pass on which
overlay owns the new ticket (the Source alone is not enough).
"""

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from django.apps import apps

from teatree.core.event_router import RoutedAction, route_event
from teatree.core.intent_classifier import classify_event
from teatree.core.reply_transport import NoopReplier, Replier
from teatree.loop.scanners.base import ScanSignal

if TYPE_CHECKING:
    from teatree.core.models.incoming_event import IncomingEvent

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class IncomingEventsScanner:
    limit: int = 25
    name: str = "incoming_events"
    replier: Replier = field(default_factory=NoopReplier)

    def scan(self) -> list[ScanSignal]:
        event_model = cast("type[IncomingEvent]", apps.get_model("core", "IncomingEvent"))
        events = event_model.objects.unprocessed().order_by("received_at", "pk")[: self.limit]
        signals: list[ScanSignal] = []
        for event in events:
            try:
                signal = self._handle(event)
            except Exception:
                logger.exception("IncomingEventsScanner failed on event %s", event.pk)
                event.mark_processed()
                continue
            event.mark_processed()
            if signal is not None:
                signals.append(signal)
        return signals

    def _handle(self, event: "IncomingEvent") -> ScanSignal | None:
        classification = classify_event(event)
        action = route_event(event, classification)
        return self._execute(event, action)

    def _execute(self, event: "IncomingEvent", action: RoutedAction) -> ScanSignal | None:
        match action.kind:
            case RoutedAction.Kind.ALERT_USER:
                self.replier.post_dm(
                    event=event,
                    actor=action.target_ref,
                    body=action.detail,
                    idempotency_key=f"incoming:{event.idempotency_key}:alert",
                )
                return ScanSignal(
                    kind="incoming_event.alert",
                    summary=f"alert from {event.source}: {action.detail}",
                    payload={"event_id": event.pk, "actor": action.target_ref},
                )
            case RoutedAction.Kind.SCHEDULE_TASK:
                return ScanSignal(
                    kind="incoming_event.task_needed",
                    summary=f"task request from {event.source} ({action.phase}): {action.detail}",
                    payload={"event_id": event.pk, "phase": action.phase, "target_ref": action.target_ref},
                )
            case RoutedAction.Kind.SCHEDULE_MERGE:
                return ScanSignal(
                    kind="incoming_event.merge_needed",
                    summary=f"merge approved on {action.target_ref} ({action.detail})",
                    payload={"event_id": event.pk, "target_ref": action.target_ref, "thread_ref": action.detail},
                )
            case RoutedAction.Kind.RECORD_ONLY:
                return ScanSignal(
                    kind="incoming_event.recorded",
                    summary=f"status update from {event.source}",
                    payload={"event_id": event.pk, "target_ref": action.target_ref},
                )
            case RoutedAction.Kind.DROP:
                return None
