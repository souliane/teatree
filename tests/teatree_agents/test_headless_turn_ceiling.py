"""The headless per-run TURN ceiling: configurable, armed by default, loud at the cap (#3890).

Cache-read cost on the headless lane is ``turns x context_size``, and the SDK
option that bounds the first factor was pinned to ``0`` — unlimited — so nothing
stopped one dispatch from re-reading a near-full context hundreds of times. These
pin the three properties that make the cap safe to arm: the ceiling is a DB-home
setting (tunable without a deploy, ``0`` still disables it), it is armed by
default, and a run that reaches it is recorded FAILED with a reason that NAMES
the ceiling **and** escalated to the owner — never a silent truncation.
"""

from types import SimpleNamespace
from unittest.mock import patch

from claude_agent_sdk import ResultMessage
from django.test import TestCase

from teatree.agents._headless_options import SpawnOverrides, _build_options, resolve_headless_max_turns
from teatree.agents.headless import _outcome_failure, _turn_ceiling
from teatree.agents.headless_truncation import (
    TURN_CEILING_SUBTYPE,
    alert_owner_max_turns_truncation,
    is_max_turns_truncation,
    max_turns_failure_reason,
)
from teatree.config import UserSettings
from teatree.core.modelkit.notify_policy import NotifyAudience
from teatree.core.models import ConfigSetting, Session, Task, TaskAttempt, Ticket
from teatree.failure_signatures import is_transient_failure


def _result(subtype: str, *, num_turns: int = 250) -> ResultMessage:
    return ResultMessage(
        subtype=subtype,
        duration_ms=1,
        duration_api_ms=1,
        is_error=subtype != "success",
        num_turns=num_turns,
        session_id="s1",
    )


class _Dispatch(TestCase):
    def _task(self, phase: str = "coding") -> Task:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket)
        return Task.objects.create(ticket=ticket, session=session, phase=phase)


class TestTheCeilingIsConfigurable(_Dispatch):
    def test_the_shipped_default_is_armed(self) -> None:
        # An unarmed default is the defect: a cap nobody sets is not a cap.
        assert UserSettings().headless_max_turns > 0
        assert resolve_headless_max_turns() == UserSettings().headless_max_turns

    def test_an_operator_row_wins_over_the_shipped_default(self) -> None:
        ConfigSetting.objects.set_value("headless_max_turns", 42, scope="")
        assert resolve_headless_max_turns() == 42

    def test_zero_disables_the_ceiling(self) -> None:
        ConfigSetting.objects.set_value("headless_max_turns", 0, scope="")
        assert resolve_headless_max_turns() == 0


class TestTheCeilingReachesTheSpawn(_Dispatch):
    def test_the_configured_ceiling_is_pinned_on_the_sdk_options(self) -> None:
        ConfigSetting.objects.set_value("headless_max_turns", 42, scope="")
        options = _build_options(self._task(), "ctx", phase="coding", skills=[])
        assert options.max_turns == 42

    def test_the_default_dispatch_is_bounded(self) -> None:
        options = _build_options(self._task(), "ctx", phase="coding", skills=[])
        assert options.max_turns == UserSettings().headless_max_turns
        assert options.max_turns > 0

    def test_zero_leaves_the_spawn_uncapped(self) -> None:
        ConfigSetting.objects.set_value("headless_max_turns", 0, scope="")
        options = _build_options(self._task(), "ctx", phase="coding", skills=[])
        assert options.max_turns == 0


class TestTheCeilingStaysOnItsOwnLane(_Dispatch):
    """``max_turns`` is not lane-neutral, so the ceiling is only pinned where it belongs.

    The neutral ``HarnessOptions`` adapter reads a positive ``max_turns`` as the CALLER's
    explicit cap and lets it WIN over a backend's own per-run limit. A ceiling chosen for
    the ``claude_sdk`` CLI lane therefore silently replaces the ``pydantic_ai`` lane's
    ``pydantic_ai_request_limit`` if it is handed to it — a cap set for one lane deciding
    another lane's budget.
    """

    def test_the_cli_spawning_backend_gets_the_ceiling(self) -> None:
        assert _turn_ceiling(_FakeHarness(spawns_cli_child=True)) == UserSettings().headless_max_turns

    def test_a_backend_with_its_own_limit_is_left_alone(self) -> None:
        assert _turn_ceiling(_FakeHarness(spawns_cli_child=False)) == 0

    def test_the_builder_pins_exactly_what_the_driver_names(self) -> None:
        options = _build_options(
            self._task(), "ctx", phase="coding", skills=[], overrides=SpawnOverrides(turn_ceiling=0)
        )
        assert options.max_turns == 0


class TestReachingTheCeilingIsVisible(_Dispatch):
    def test_the_cap_subtype_is_recognised(self) -> None:
        assert is_max_turns_truncation(_result(TURN_CEILING_SUBTYPE)) is True
        assert is_max_turns_truncation(_result("success")) is False
        assert is_max_turns_truncation(None) is False

    def test_a_capped_run_is_recorded_failed_naming_the_ceiling(self) -> None:
        task = self._task()
        with patch("teatree.agents.headless.alert_owner_max_turns_truncation"):
            attempt = _outcome_failure(task, _harness_outcome(_result(TURN_CEILING_SUBTYPE)), phase="coding")
        assert attempt is not None
        assert attempt.exit_code == 1
        assert attempt.task_id == task.pk
        # The reason must name the ceiling, the subtype, and the knob that moves it,
        # so the truncation is diagnosable from the recorded attempt alone.
        assert "turn ceiling" in attempt.error
        assert TURN_CEILING_SUBTYPE in attempt.error
        assert "headless_max_turns" in attempt.error
        assert TaskAttempt.objects.filter(pk=attempt.pk).exists()

    def test_a_capped_run_escalates_to_the_owner(self) -> None:
        task = self._task()
        with patch("teatree.agents.headless_truncation.notify_user", return_value=True) as notify:
            _outcome_failure(task, _harness_outcome(_result(TURN_CEILING_SUBTYPE)), phase="coding")
        notify.assert_called_once()
        kwargs = notify.call_args.kwargs
        assert kwargs["audience"] is NotifyAudience.OWNER_ESCALATION
        assert kwargs["idempotency_key"] == f"max-turns-truncation:{task.pk}:coding"

    def test_the_cap_is_not_laundered_into_a_transient_auto_requeue(self) -> None:
        # A turn ceiling is a DELIBERATE bound, not an infrastructure interruption.
        # Recorded under the generic ``result_error:`` prefix it would match the
        # transient marker set and be auto-requeued straight back into the same
        # ceiling; the named reason keeps it deterministic, so the repair sweep
        # escalates it durably instead of silently re-spending the run.
        task = self._task()
        with patch("teatree.agents.headless.alert_owner_max_turns_truncation"):
            attempt = _outcome_failure(task, _harness_outcome(_result(TURN_CEILING_SUBTYPE)), phase="coding")
        assert attempt is not None
        assert is_transient_failure(attempt.error) is False

    def test_the_reason_names_the_turns_and_both_lanes_ceilings(self) -> None:
        # ONE subtype covers both lanes' per-run caps, so the reason names both knobs
        # rather than guessing from ambient config which lane produced the message.
        reason = max_turns_failure_reason(_result(TURN_CEILING_SUBTYPE, num_turns=311))
        assert "311 turns" in reason
        assert "headless_max_turns" in reason
        assert "pydantic_ai_request_limit" in reason

    def test_the_owner_alert_never_masks_the_recorded_failure(self) -> None:
        # Best-effort egress: a broken notification must not swallow the FAILED record.
        task = self._task()
        with patch("teatree.agents.headless_truncation.notify_user", side_effect=RuntimeError):
            alert_owner_max_turns_truncation(task, phase="coding", message=_result(TURN_CEILING_SUBTYPE))

    def test_a_healthy_run_neither_fails_nor_escalates(self) -> None:
        task = self._task()
        with patch("teatree.agents.headless_truncation.notify_user", return_value=True) as notify:
            assert _outcome_failure(task, _harness_outcome(_result("success")), phase="coding") is None
        notify.assert_not_called()


class _FakeHarness:
    """The narrow slice of the harness protocol the ceiling resolver reads."""

    def __init__(self, *, spawns_cli_child: bool) -> None:
        self.capabilities = SimpleNamespace(spawns_cli_child=spawns_cli_child)


def _harness_outcome(message: ResultMessage):
    from teatree.agents.headless import HarnessOutcome  # noqa: PLC0415 — deferred: keeps the module import light

    return HarnessOutcome(agent_text="", result_message=message, stuck_reason=None)
