"""The doctor's readings of the metered lane — unset ceiling, unknown spend, split harness (#4816).

Three unknowns the governor cannot resolve for itself, each made VISIBLE rather than
silently wrong: a lane with no budget, a budget that is only a floor, and a per-overlay
lane the whole-box probe deliberately does not model.
"""

import io
from contextlib import redirect_stdout
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from teatree.cli.doctor.checks_metered_lane import (
    check_metered_lane_ceiling,
    check_metered_usage_unknown,
    check_overlay_harness_agreement,
)
from teatree.core.metered_spend import MeteredSpend
from teatree.core.models import Session, Task, TaskAttempt, Ticket
from teatree.core.models.config_setting import ConfigSetting

_UNREADABLE = MeteredSpend(fresh=False, tokens=0, ceiling=0, unknown_attempts=0, estimated_cost_usd=0.0, window_hours=0)


def _run(check) -> str:
    """Run *check* and return what it printed — the doctor's surface IS its output."""
    stream = io.StringIO()
    with redirect_stdout(stream):
        assert check() is True, "every metered-lane reading is advisory and never reddens the run"
    return stream.getvalue()


def _harnesses(by_overlay: dict[str, str]):
    return patch("teatree.cli.doctor.checks_metered_lane._harness_by_overlay", return_value=dict(by_overlay))


class MeteredCeilingDoctorCheckTests(TestCase):
    """A metered lane with no ceiling is the unbounded state four runs walked into."""

    def _on_the_metered_lane(self) -> None:
        # The cross-key rule binds the provider to its transport, so both are set: this is
        # the router-key shape the ticket's own 307 refusals came from.
        ConfigSetting.objects.set_value("agent_harness", "pydantic_ai")
        ConfigSetting.objects.set_value("agent_harness_provider", "openai_compatible")

    def test_a_metered_lane_with_no_ceiling_warns(self) -> None:
        self._on_the_metered_lane()
        ConfigSetting.objects.set_value("metered_token_ceiling", 0)

        output = _run(check_metered_lane_ceiling)

        assert "WARN" in output
        assert "metered_token_ceiling" in output

    def test_control_a_configured_ceiling_is_silent(self) -> None:
        self._on_the_metered_lane()
        ConfigSetting.objects.set_value("metered_token_ceiling", 1_000_000)

        assert "WARN" not in _run(check_metered_lane_ceiling)

    def test_control_a_subscription_lane_needs_no_metered_ceiling(self) -> None:
        ConfigSetting.objects.set_value("agent_harness_provider", "subscription_oauth")
        ConfigSetting.objects.set_value("metered_token_ceiling", 0)

        assert "WARN" not in _run(check_metered_lane_ceiling)


class MeteredUnknownUsageDoctorCheckTests(TestCase):
    """An attempt whose spend was unreadable contributes nothing, so the total under-reads."""

    def _metered_attempt(self, *, usage_unknown: bool) -> None:
        ticket = Ticket.objects.create(role="author")
        session = Session.objects.create(ticket=ticket)
        task = Task.objects.create(ticket=ticket, session=session, phase="coding")
        TaskAttempt.objects.create(
            task=task,
            lane=TaskAttempt.Lane.METERED,
            ended_at=timezone.now(),
            usage_unknown=usage_unknown,
        )

    def test_unknown_usage_in_the_window_warns_that_the_total_is_a_floor(self) -> None:
        self._metered_attempt(usage_unknown=True)

        output = _run(check_metered_usage_unknown)

        assert "WARN" in output
        assert "FLOOR" in output

    def test_control_a_window_of_measured_attempts_is_silent(self) -> None:
        self._metered_attempt(usage_unknown=False)

        assert "WARN" not in _run(check_metered_usage_unknown)

    def test_control_an_unreadable_ledger_never_warns(self) -> None:
        # An unreadable ledger reports no unknown attempts AND no known ones; warning on
        # it would turn a read failure into a claim about spend.
        self._metered_attempt(usage_unknown=True)
        with patch("teatree.core.metered_spend.read_metered_spend", return_value=_UNREADABLE):
            assert "WARN" not in _run(check_metered_usage_unknown)


class OverlayHarnessAgreementDoctorCheckTests(TestCase):
    """The governor probes ONCE per drain, so a per-overlay lane would be silently wrong."""

    def test_disagreeing_overlays_warn_that_the_lane_is_not_modelled(self) -> None:
        with _harnesses({"alpha": "claude_sdk", "beta": "pydantic_ai"}):
            output = _run(check_overlay_harness_agreement)

        assert "WARN" in output
        assert "alpha" in output
        assert "beta" in output

    def test_control_agreeing_overlays_are_silent(self) -> None:
        with _harnesses({"alpha": "pydantic_ai", "beta": "pydantic_ai"}):
            assert "WARN" not in _run(check_overlay_harness_agreement)

    def test_control_a_single_overlay_is_silent(self) -> None:
        with _harnesses({"alpha": "pydantic_ai"}):
            assert "WARN" not in _run(check_overlay_harness_agreement)

    def test_control_an_unreadable_overlay_registry_never_warns(self) -> None:
        with patch(
            "teatree.cli.doctor.checks_metered_lane._harness_by_overlay",
            side_effect=RuntimeError("entry points unreadable"),
        ):
            assert "WARN  Registered overlays disagree" not in _run(check_overlay_harness_agreement)
