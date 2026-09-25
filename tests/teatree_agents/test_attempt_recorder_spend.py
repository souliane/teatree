"""#4164: a FAILED attempt records the spend its run already billed; a park records NULL."""

import json

import pytest
from django.test import TestCase

from teatree.agents.runner import HarnessOutcome, _outcome_failure, _record_success
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


class TestPostTurnParkRecordsItsSpend(SpendRecordingCase):
    """A usage-limit park is reached AFTER the SDK returned a result — it billed a turn.

    ``_outcome_failure`` samples ``usage`` from the SAME ``ResultMessage`` every branch
    classifies (including the limit branch) — a park must carry it through to
    ``TaskAttempt`` like every other branch, not discard it as though nothing ran.
    """

    def test_a_rate_limited_run_parks_but_still_records_its_spend(self) -> None:
        task = self.make_task()
        task.claim(claimed_by="headless-worker")
        outcome = HarnessOutcome(
            agent_text="",
            result_message=result_message(
                is_error=True, result="rate limit exceeded", num_turns=3, usage=_USAGE, total_cost_usd=0.42
            ),
            stuck_reason=None,
        )

        attempt = _outcome_failure(task, outcome, phase="coding", lane=TaskAttempt.Lane.SUBSCRIPTION)

        assert attempt is not None
        task.refresh_from_db()
        assert task.status == Task.Status.PENDING, "parked, not FAILED"
        assert attempt.input_tokens == 4200
        assert attempt.output_tokens == 310
        assert attempt.cache_read_tokens == 90
        assert attempt.cost_usd == pytest.approx(0.42)


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
        from teatree.agents.runner import _record_failure  # noqa: PLC0415 — the recorder under test

        task = self.make_task()

        attempt = _record_failure(task, error="claude is not installed")

        assert attempt.input_tokens is None
        assert attempt.cost_usd is None
        assert attempt.lane == ""


_PER_REQUEST = [
    {
        "model": "z-ai/glm-5.3-flash",
        "prompt_tokens": 100,
        "completion_tokens": 5,
        "cached_tokens": 0,
        "cost_usd": 4e-05,
    },
    {"model": "", "prompt_tokens": None, "completion_tokens": None, "cached_tokens": None, "cost_usd": None},
]


_TRAJECTORY = [
    {"tool": "Read", "arg_keys": ["path"], "args_bytes": 16, "output_bytes": 812, "duration_ms": 4},
    {"tool": "Bash", "arg_keys": ["command"], "args_bytes": 21, "output_bytes": None, "duration_ms": None},
]


class TestPerRequestUsageIsKeptOnTheAttempt(SpendRecordingCase):
    """The transport's per-request usage lands in ``TaskAttempt.result``, whatever the outcome."""

    def test_a_completed_run_keeps_its_per_request_usage_beside_its_envelope(self) -> None:
        ticket = Ticket.objects.create(overlay="test", role=Ticket.Role.AUTHOR)
        task = Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase="retro")
        outcome = HarnessOutcome(
            agent_text=json.dumps({"summary": "done"}),
            result_message=result_message(
                usage={**_USAGE, "per_request": _PER_REQUEST, "tool_calls": _TRAJECTORY}, total_cost_usd=4e-05
            ),
            stuck_reason=None,
        )

        attempt = _record_success(task, outcome, phase="retro", lane=TaskAttempt.Lane.METERED)

        attempt.refresh_from_db()
        assert attempt.error == ""
        assert attempt.result == {"summary": "done", "usage_per_request": _PER_REQUEST, "tool_calls": _TRAJECTORY}
        assert attempt.cost_is_estimated is False

    def test_a_refused_run_keeps_the_per_request_usage_it_billed(self) -> None:
        outcome = HarnessOutcome(
            agent_text="",
            result_message=result_message(
                is_error=True,
                subtype="error_during_execution",
                result="status_code: 400, model_name: m, body: bad request",
                api_error_status=400,
                usage={**_USAGE, "per_request": _PER_REQUEST, "tool_calls": _TRAJECTORY},
            ),
            stuck_reason=None,
        )

        attempt = _outcome_failure(self.make_task(), outcome, phase="coding", lane=TaskAttempt.Lane.METERED)

        assert attempt is not None
        attempt.refresh_from_db()
        assert attempt.result["usage_per_request"] == _PER_REQUEST
        assert attempt.result["tool_calls"] == _TRAJECTORY, "a failed run keeps the trajectory it made"

    def test_a_run_whose_transport_measured_nothing_records_no_per_request_key(self) -> None:
        attempt = _outcome_failure(
            self.make_task(), self.failed_outcome(), phase="coding", lane=TaskAttempt.Lane.SUBSCRIPTION
        )

        assert attempt is not None
        assert "usage_per_request" not in attempt.result
        assert "tool_calls" not in attempt.result


class TestSpendUnknownIsDistinguishableFromNothingBilled(SpendRecordingCase):
    """NULL meant BOTH "nothing was billed" and "turns ran, spend unknown" (#4816).

    Those demand opposite readings — one is a free park, the other is money the
    metered ledger cannot see — so the ledger needs a positive marker for the second.
    """

    def test_turns_ran_with_no_provider_usage_is_marked_unknown_never_zero(self) -> None:
        task = self.make_task()
        outcome = HarnessOutcome(
            agent_text="",
            result_message=result_message(
                is_error=True, subtype="error_during_execution", num_turns=5, usage=None, total_cost_usd=None
            ),
            stuck_reason=None,
        )

        attempt = _outcome_failure(task, outcome, phase="coding", lane=TaskAttempt.Lane.METERED)

        assert attempt is not None
        assert attempt.usage_unknown is True
        assert attempt.input_tokens is None
        assert attempt.output_tokens is None

    def test_a_stream_that_died_before_its_terminal_message_is_marked_unknown(self) -> None:
        from teatree.agents.runner_usage import (  # noqa: PLC0415 — the mapper under test
            UsageObservation,
            _attempt_usage,
        )

        usage = _attempt_usage(None, UsageObservation(lane=TaskAttempt.Lane.METERED))

        assert usage.usage_unknown is True
        assert usage.input_tokens is None

    def test_control_a_pre_turn_refusal_is_null_but_not_unknown(self) -> None:
        from teatree.agents.runner import _record_failure  # noqa: PLC0415 — the recorder under test

        task = self.make_task()

        attempt = _record_failure(task, error="claude is not installed")

        assert attempt.input_tokens is None
        assert attempt.usage_unknown is False

    def test_control_a_measured_run_is_neither_null_nor_unknown(self) -> None:
        task = self.make_task()

        attempt = _outcome_failure(task, self.failed_outcome(), phase="coding", lane=TaskAttempt.Lane.METERED)

        assert attempt is not None
        assert attempt.input_tokens == 4200
        assert attempt.usage_unknown is False

    def test_control_a_provider_reported_zero_stays_a_measurement(self) -> None:
        task = self.make_task()
        outcome = HarnessOutcome(
            agent_text="",
            result_message=result_message(
                is_error=True,
                subtype="error_during_execution",
                num_turns=1,
                usage={"input_tokens": 0, "output_tokens": 0},
                total_cost_usd=None,
            ),
            stuck_reason=None,
        )

        attempt = _outcome_failure(task, outcome, phase="coding", lane=TaskAttempt.Lane.METERED)

        assert attempt is not None
        assert attempt.input_tokens == 0
        assert attempt.usage_unknown is False

    def test_no_recorder_writes_a_zero_the_provider_did_not_report(self) -> None:
        """The ticket's stated criterion, over every recorder path this module exercises."""
        from teatree.agents.runner import _record_failure  # noqa: PLC0415 — one of the paths swept

        unmeasured = [
            _record_failure(self.make_task(), error="claude is not installed"),
            _outcome_failure(
                self.make_task(),
                HarnessOutcome(
                    agent_text="",
                    result_message=result_message(is_error=True, subtype="error_during_execution", num_turns=3),
                    stuck_reason=None,
                ),
                phase="coding",
                lane=TaskAttempt.Lane.METERED,
            ),
        ]

        for attempt in unmeasured:
            assert attempt is not None
            assert attempt.input_tokens is None, "an unreported token count must stay NULL, never 0"
            assert attempt.output_tokens is None
            assert attempt.cost_usd is None


class TestTheOutermostCrashWriterMarksItsSpendUnknown(SpendRecordingCase):
    """``complete_with_attempt`` is reached from an exception that ESCAPED the drive.

    Core cannot import the agent layer to parse a result message, so the spend is
    genuinely unreadable there — which is exactly the state that needs saying out loud.
    """

    def test_a_harness_crash_records_unknown_usage(self) -> None:
        task = self.make_task()

        attempt = task.complete_with_attempt(exit_code=1, error="Traceback…", usage_unknown=True)

        assert attempt.usage_unknown is True
        assert attempt.input_tokens is None

    def test_control_a_deterministic_phase_completion_bills_nothing_and_says_so(self) -> None:
        task = self.make_task()

        attempt = task.complete_with_attempt(exit_code=0, result={"summary": "done"})

        assert attempt.usage_unknown is False
        assert attempt.input_tokens is None
