"""#4164: a FAILED attempt records the spend its run already billed; a park records NULL."""

import pytest
from django.test import TestCase

from teatree.agents.headless import HarnessOutcome, _outcome_failure
from teatree.core.models import Session, Task, TaskAttempt, Ticket
from tests.teatree_agents._sdk_fake import result_message

_USAGE = {"input_tokens": 4200, "output_tokens": 310, "cache_read_input_tokens": 90}


class SpendRecordingCase(TestCase):
    def make_task(self) -> Task:
        ticket = Ticket.objects.create(overlay="test", role=Ticket.Role.AUTHOR)
        return Task.objects.create(
            ticket=ticket,
            session=Session.objects.create(ticket=ticket, overlay="test"),
            phase="coding",
        )

    def failed_outcome(self, **overrides: object) -> HarnessOutcome:
        message = result_message(
            is_error=True,
            subtype="error_during_execution",
            num_turns=7,
            total_cost_usd=0.42,
            usage=_USAGE,
            model_usage={"claude-opus-5": {}},
            **overrides,
        )
        return HarnessOutcome(agent_text="", result_message=message, stuck_reason=None)


class TestFailureRecordsItsSpend(SpendRecordingCase):
    def test_a_failed_run_records_the_tokens_it_billed(self) -> None:
        """The anti-vacuity anchor: this field was unconditionally NULL on every failure."""
        task = self.make_task()

        attempt = _outcome_failure(task, self.failed_outcome(), phase="coding", lane=TaskAttempt.Lane.SUBSCRIPTION)

        assert attempt is not None
        assert attempt.error
        assert attempt.input_tokens == 4200
        assert attempt.output_tokens == 310
        assert attempt.cache_read_tokens == 90
        assert attempt.num_turns == 7
        assert attempt.cost_usd == pytest.approx(0.42)
        assert attempt.lane == TaskAttempt.Lane.SUBSCRIPTION
        assert attempt.model == "claude-opus-5"

    def test_a_turn_ceiling_truncation_records_its_spend(self) -> None:
        """The ceiling is reached BY spending; a NULL there understates the cost of it."""
        task = self.make_task()
        outcome = HarnessOutcome(
            agent_text="",
            result_message=result_message(
                subtype="error_max_turns", is_error=True, num_turns=99, usage=_USAGE, total_cost_usd=1.5
            ),
            stuck_reason=None,
        )

        attempt = _outcome_failure(task, outcome, phase="coding", lane=TaskAttempt.Lane.METERED)

        assert attempt is not None
        assert attempt.input_tokens == 4200
        assert attempt.lane == TaskAttempt.Lane.METERED

    def test_a_stuck_run_records_its_spend(self) -> None:
        task = self.make_task()
        outcome = HarnessOutcome(
            agent_text="",
            result_message=result_message(usage=_USAGE, total_cost_usd=0.9),
            stuck_reason="runtime ceiling breached",
        )

        attempt = _outcome_failure(task, outcome, phase="coding", lane=TaskAttempt.Lane.SUBSCRIPTION)

        assert attempt is not None
        assert attempt.input_tokens == 4200
        assert attempt.cost_usd == pytest.approx(0.9)


class TestPreTurnFailureStaysNull(SpendRecordingCase):
    def test_a_pre_turn_park_records_null_not_zero(self) -> None:
        """A zero would be a WORSE lie than a NULL — it reads as a measurement."""
        from datetime import timedelta  # noqa: PLC0415 — local to the one case that parks

        from django.utils import timezone  # noqa: PLC0415 — local to the one case that parks

        from teatree.agents.usage_window import _record_park  # noqa: PLC0415 — the park recorder under test

        task = self.make_task()
        task.claim(claimed_by="headless-worker")

        attempt = _record_park(task, reason="limit_parked: window", not_before=timezone.now() + timedelta(hours=1))

        assert attempt.input_tokens is None
        assert attempt.output_tokens is None
        assert attempt.cost_usd is None
        assert attempt.lane == ""

    def test_a_pre_dispatch_refusal_records_null(self) -> None:
        from teatree.agents.headless import _record_failure  # noqa: PLC0415 — the recorder under test

        task = self.make_task()

        attempt = _record_failure(task, error="claude is not installed")

        assert attempt.input_tokens is None
        assert attempt.cost_usd is None
        assert attempt.lane == ""
