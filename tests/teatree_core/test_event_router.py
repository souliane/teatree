"""Behaviour tests for the IncomingEvent → action router (#654 phase 3)."""

from django.test import TestCase

from teatree.core.event_router import RoutedAction, route_event
from teatree.core.intent_classifier import classify_event
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
