"""Route an ``IncomingEvent`` + ``IntentClassification`` to a concrete action.

Sits between the classifier (#654 phase 2) and the executor (loop tick /
agent task pickup / Replier post). The router is a pure function — it
produces a ``RoutedAction`` value and never mutates state. The caller
turns the value into the right side effect: ``schedule_task`` enqueues a
``Task``, ``alert_user`` posts via the ``Replier``, ``record_only``
marks the event processed without further work.

Reference: #654 § "Action router".
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar

from teatree.core.models import IncomingEvent, IntentClassification


class _Kind(StrEnum):
    SCHEDULE_TASK = "schedule_task"
    SCHEDULE_MERGE = "schedule_merge"
    ALERT_USER = "alert_user"
    RECORD_ONLY = "record_only"
    DROP = "drop"


@dataclass(slots=True)
class RoutedAction:
    Kind: ClassVar = _Kind

    kind: _Kind
    target_ref: str = ""
    phase: str = ""
    detail: str = ""


_INTENT_TO_PHASE = {
    IntentClassification.Intent.TASK: "coding",
    IntentClassification.Intent.QUESTION: "answering",
}


def route_event(event: IncomingEvent, classification: IntentClassification) -> RoutedAction:
    """Route an event to a concrete action; a pure value, never a side effect.

    A ``DIRECTIVE`` intent is unrouteable and DROPs (#105): ambient directive detection
    is deleted, so the only ``Directive`` producer is the explicit
    ``Directive.objects.capture`` (the CLI + ``/t3:directive`` path, ``source=CLI``).
    """
    intent = classification.intent
    if intent in _INTENT_TO_PHASE:
        return RoutedAction(
            kind=RoutedAction.Kind.SCHEDULE_TASK,
            phase=_INTENT_TO_PHASE[intent],
            target_ref=event.channel_ref,
            detail=event.body[:255],
        )
    if intent == IntentClassification.Intent.APPROVAL:
        return RoutedAction(
            kind=RoutedAction.Kind.SCHEDULE_MERGE,
            target_ref=event.channel_ref,
            detail=event.thread_ref,
        )
    if intent == IntentClassification.Intent.ESCALATION:
        return RoutedAction(
            kind=RoutedAction.Kind.ALERT_USER,
            target_ref=event.actor or event.channel_ref,
            detail=f"urgent: {event.body[:200]}",
        )
    if intent == IntentClassification.Intent.STATUS_UPDATE:
        return RoutedAction(kind=RoutedAction.Kind.RECORD_ONLY, target_ref=event.channel_ref)
    return RoutedAction(kind=RoutedAction.Kind.DROP)
