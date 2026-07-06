"""Behaviour tests for the IncomingEvent → action router (#654 phase 3)."""

from django.test import TestCase

from teatree.core.intake.event_router import RoutedAction, route_event
from teatree.core.intake.intent_classifier import classify_event
from teatree.core.models import IncomingEvent, IntentClassification


def _event(source: str, *, body: str, key: str, **payload_extras: object) -> IncomingEvent:
    return IncomingEvent.objects.create(
        source=source,
        actor="alice",
        channel_ref="org/repo",
        thread_ref="thread-1",
        body=body,
        payload_json=payload_extras,
        idempotency_key=key,
    )


class TestRouteEvent(TestCase):
    def test_task_intent_schedules_coding_task(self) -> None:
        event = _event(
            IncomingEvent.Source.SLACK,
            body="<@bot> please implement the dashboard",
            key="slack:t1",
            event={"type": "app_mention"},
        )
        classification = classify_event(event)
        assert classification.intent == IntentClassification.Intent.TASK

        action = route_event(event, classification)

        assert action.kind == RoutedAction.Kind.SCHEDULE_TASK
        assert action.phase == "coding"
        assert action.target_ref == event.channel_ref

    def test_question_intent_schedules_answerer(self) -> None:
        event = _event(
            IncomingEvent.Source.SLACK,
            body="<@bot> what's the status of !42?",
            key="slack:q1",
            event={"type": "app_mention"},
        )
        classification = classify_event(event)

        action = route_event(event, classification)

        assert action.kind == RoutedAction.Kind.SCHEDULE_TASK
        assert action.phase == "answering"

    def test_approval_intent_schedules_merge(self) -> None:
        event = _event(
            IncomingEvent.Source.GITLAB,
            body="approved",
            key="gitlab:a1",
            object_kind="merge_request",
            object_attributes={"action": "approved", "iid": 42},
        )
        classification = classify_event(event)

        action = route_event(event, classification)

        assert action.kind == RoutedAction.Kind.SCHEDULE_MERGE
        assert action.target_ref == event.channel_ref

    def test_status_update_intent_is_recorded_only(self) -> None:
        event = _event(
            IncomingEvent.Source.CI,
            body="pipeline succeeded",
            key="ci:1",
            status="success",
        )
        classification = classify_event(event)

        action = route_event(event, classification)

        assert action.kind == RoutedAction.Kind.RECORD_ONLY

    def test_escalation_intent_posts_user_alert(self) -> None:
        event = _event(
            IncomingEvent.Source.SLACK,
            body="<@bot> urgent: prod is down",
            key="slack:e1",
            event={"type": "app_mention"},
        )
        classification = classify_event(event)

        action = route_event(event, classification)

        assert action.kind == RoutedAction.Kind.ALERT_USER
        assert "urgent" in action.detail.lower()

    def test_noise_intent_is_dropped(self) -> None:
        event = _event(
            IncomingEvent.Source.SLACK,
            body="",
            key="slack:n1",
            event={"type": "team_join"},
        )
        classification = classify_event(event)

        action = route_event(event, classification)

        assert action.kind == RoutedAction.Kind.DROP


class TestDirectiveRouting(TestCase):
    """North-star PR-6: a DIRECTIVE intent captures only when directive routing is on."""

    def _directive_event(self) -> tuple[IncomingEvent, IntentClassification]:
        event = _event(
            IncomingEvent.Source.SLACK,
            body="<@bot> always open MRs as drafts for overlay X",
            key="slack:d1",
            event={"type": "app_mention"},
        )
        classification = IntentClassification.objects.create(
            event=event, intent=IntentClassification.Intent.DIRECTIVE, confidence=0.9
        )
        return event, classification

    def test_directive_intent_is_dropped_while_routing_is_off_flag_off_parity(self) -> None:
        event, classification = self._directive_event()
        action = route_event(event, classification)  # default: directive_routing_enabled=False
        assert action.kind == RoutedAction.Kind.DROP

    def test_directive_intent_captures_when_routing_is_enabled(self) -> None:
        event, classification = self._directive_event()
        action = route_event(event, classification, directive_routing_enabled=True)
        assert action.kind == RoutedAction.Kind.CAPTURE_DIRECTIVE
        assert action.target_ref == event.channel_ref
        assert "drafts" in action.detail
