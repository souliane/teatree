"""Behaviour tests for the IncomingEvent intent classifier (#654 phase 2)."""

import pytest
from django.test import TestCase

from teatree.core.intent_classifier import classify_event
from teatree.core.models import IncomingEvent, IntentClassification


def _slack_event(text: str, **overrides: object) -> IncomingEvent:
    payload = {
        "source": IncomingEvent.Source.SLACK,
        "actor": "U02ABC",
        "channel_ref": "C0LIST",
        "thread_ref": "1234.0001",
        "body": text,
        "payload_json": {"event": {"type": "app_mention", "text": text}},
        "idempotency_key": f"slack:Ev{abs(hash(text))}",
    }
    payload.update(overrides)
    return IncomingEvent.objects.create(**payload)


def _gitlab_event(action: str, **overrides: object) -> IncomingEvent:
    payload = {
        "source": IncomingEvent.Source.GITLAB,
        "actor": "U_GL",
        "channel_ref": "org/repo",
        "thread_ref": "",
        "body": f"MR action: {action}",
        "payload_json": {"object_kind": "merge_request", "object_attributes": {"action": action}},
        "idempotency_key": f"gitlab:{abs(hash(action))}",
    }
    payload.update(overrides)
    return IncomingEvent.objects.create(**payload)


class TestClassifyEvent(TestCase):
    def test_question_intent_for_mention_with_question_mark(self) -> None:
        event = _slack_event("<@U0LAN0Z89> what's the status of the release?")

        classification = classify_event(event)

        assert classification.intent == IntentClassification.Intent.QUESTION
        assert classification.event == event
        assert classification.confidence >= 0.8

    def test_task_intent_for_imperative_mention(self) -> None:
        event = _slack_event("<@U0LAN0Z89> can you implement the dashboard hot-reload?")

        classification = classify_event(event)

        assert classification.intent == IntentClassification.Intent.TASK

    def test_approval_intent_for_gitlab_approved_event(self) -> None:
        event = _gitlab_event(
            "approved",
            payload_json={
                "object_kind": "merge_request",
                "object_attributes": {"action": "approved", "iid": 42},
            },
        )

        classification = classify_event(event)

        assert classification.intent == IntentClassification.Intent.APPROVAL

    def test_status_update_intent_for_ci_pipeline(self) -> None:
        event = IncomingEvent.objects.create(
            source=IncomingEvent.Source.CI,
            actor="ci-bot",
            channel_ref="pipeline/123",
            body="pipeline succeeded",
            payload_json={"object_kind": "pipeline", "status": "success"},
            idempotency_key="ci:pipe-123",
        )

        classification = classify_event(event)

        assert classification.intent == IntentClassification.Intent.STATUS_UPDATE

    def test_escalation_intent_for_urgent_mention(self) -> None:
        event = _slack_event("<@U0LAN0Z89> urgent: prod is down, need eyes now")

        classification = classify_event(event)

        assert classification.intent == IntentClassification.Intent.ESCALATION

    def test_noise_intent_for_unparseable_event(self) -> None:
        event = IncomingEvent.objects.create(
            source=IncomingEvent.Source.SLACK,
            actor="",
            channel_ref="",
            body="",
            payload_json={"event": {"type": "team_join"}},
            idempotency_key="slack:join-1",
        )

        classification = classify_event(event)

        assert classification.intent == IntentClassification.Intent.NOISE

    def test_classification_is_persisted_and_linked_to_event(self) -> None:
        event = _slack_event("<@U0LAN0Z89> hey, can you help?")

        classification = classify_event(event)

        assert classification.pk is not None
        event.refresh_from_db()
        assert event.classified_as.pk == classification.pk

    def test_reclassifying_returns_existing_classification(self) -> None:
        event = _slack_event("<@U0LAN0Z89> what now?")

        first = classify_event(event)
        second = classify_event(event)

        assert first.pk == second.pk
        assert IntentClassification.objects.filter(event=event).count() == 1


class TestMalformedWebhookSubObjects(TestCase):
    """A truthy non-dict webhook sub-object must not raise; it is treated as absent."""

    def test_gitlab_object_attributes_as_list_does_not_raise(self) -> None:
        event = IncomingEvent.objects.create(
            source=IncomingEvent.Source.GITLAB,
            actor="U_GL",
            channel_ref="org/repo",
            body="",
            payload_json={"object_kind": "merge_request", "object_attributes": ["approved"]},
            idempotency_key="gitlab:malformed-attrs",
        )

        classification = classify_event(event)

        assert classification.intent == IntentClassification.Intent.NOISE

    def test_github_review_as_string_does_not_raise(self) -> None:
        event = IncomingEvent.objects.create(
            source=IncomingEvent.Source.GITHUB,
            actor="U_GH",
            channel_ref="org/repo",
            body="",
            payload_json={"review": "approved"},
            idempotency_key="github:malformed-review",
        )

        classification = classify_event(event)

        assert classification.intent == IntentClassification.Intent.NOISE

    def test_slack_event_as_list_does_not_raise(self) -> None:
        event = IncomingEvent.objects.create(
            source=IncomingEvent.Source.SLACK,
            actor="U02ABC",
            channel_ref="C0LIST",
            body="",
            payload_json={"event": ["app_mention"]},
            idempotency_key="slack:malformed-event",
        )

        classification = classify_event(event)

        assert classification.intent == IntentClassification.Intent.NOISE


class TestIntentClassificationModel(TestCase):
    def test_intent_choices_match_issue_spec(self) -> None:
        names = {choice for choice, _label in IntentClassification.Intent.choices}
        # ``directive`` is the north-star PR-6 standing-constraint intent (routed to a
        # Directive capture only when directive routing is enabled).
        assert names == {"task", "question", "approval", "status_update", "escalation", "directive", "noise"}

    def test_confidence_must_be_between_zero_and_one(self) -> None:
        from django.core.exceptions import ValidationError  # noqa: PLC0415

        event = IncomingEvent.objects.create(
            source=IncomingEvent.Source.SLACK,
            body="x",
            idempotency_key="slack:val-1",
        )

        invalid = IntentClassification(
            event=event,
            intent=IntentClassification.Intent.QUESTION,
            confidence=1.5,
            rationale="too high",
        )
        with pytest.raises(ValidationError):
            invalid.full_clean()
