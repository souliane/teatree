"""Tests for teatree.agents.headless_truncation — max-tokens truncation alerting."""

from unittest.mock import patch

from claude_agent_sdk import ResultMessage
from django.test import TestCase

from teatree.agents.headless_truncation import alert_owner_max_tokens_truncation, is_max_tokens_truncation
from teatree.agents.pydantic_ai_session import MAX_TOKENS_TRUNCATION_SUBTYPE
from teatree.core.modelkit.notify_policy import NotifyAudience
from teatree.core.models import Session, Task, Ticket
from teatree.core.notify import NotifyKind


def _result(subtype: str) -> ResultMessage:
    return ResultMessage(
        subtype=subtype,
        duration_ms=1,
        duration_api_ms=1,
        is_error=subtype != "success",
        num_turns=1,
        session_id="s1",
    )


class IsMaxTokensTruncationTests(TestCase):
    def test_true_only_for_the_truncation_subtype(self) -> None:
        assert is_max_tokens_truncation(_result(MAX_TOKENS_TRUNCATION_SUBTYPE)) is True

    def test_false_for_other_subtypes_and_none(self) -> None:
        assert is_max_tokens_truncation(_result("success")) is False
        assert is_max_tokens_truncation(None) is False


class AlertOwnerMaxTokensTruncationTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=self.ticket, agent_id="agent-1")
        self.task = Task.objects.create(ticket=self.ticket, session=session, phase="coding")

    def test_dms_the_owner_through_the_audited_escalation_egress(self) -> None:
        with patch("teatree.agents.headless_truncation.notify_user", return_value=True) as notify:
            alert_owner_max_tokens_truncation(self.task, phase="coding")
        notify.assert_called_once()
        kwargs = notify.call_args.kwargs
        assert kwargs["kind"] is NotifyKind.INFO
        assert kwargs["audience"] is NotifyAudience.OWNER_ESCALATION
        assert kwargs["idempotency_key"] == f"max-tokens-truncation:{self.task.pk}:coding"

    def test_never_raises_when_the_egress_fails(self) -> None:
        with patch("teatree.agents.headless_truncation.notify_user", side_effect=RuntimeError("egress down")):
            # best-effort: a failure in the alert must not mask the recorded failure
            alert_owner_max_tokens_truncation(self.task, phase="coding")
