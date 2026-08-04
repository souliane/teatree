"""Governor gating of the INTERACTIVE dispatch chokepoint (#4107).

``decide_admission`` had exactly two callers, both factory lanes
(``core.headless_admission``, ``loop.admission``), so an orchestrator session
dispatching through the ``Agent``/``Task`` tool was admitted with no ceiling and
no load brake — the two agent populations summed unchecked (measured: load 58 on
8 cores while ``issue_implementer_max_concurrent = 3`` was in force). These cover
the core-side seam the dispatch gate consults.
"""

import datetime as dt
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from unittest.mock import patch

import pytest
from django.test import TestCase
from django.utils import timezone

from teatree.core import dispatch_admission as gate_mod
from teatree.core.admission_governor import BRAKE_LOAD_PER_CORE, MachineSignal, QuotaSignal
from teatree.core.dispatch_admission import dispatch_admission_denied_reason
from teatree.core.models import Session, Task, Ticket

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

_WEEK = 7 * 24 * 3600
_CORES = 8
_OVER_THE_WATERMARK = BRAKE_LOAD_PER_CORE * _CORES + 1


def _healthy_quota() -> QuotaSignal:
    return QuotaSignal(
        fresh=True,
        all_accounts_exhausted=False,
        weekly_utilization=0.1,
        short_utilization=0.1,
        seconds_to_weekly_reset=_WEEK * 0.5,
    )


def _unknown_quota() -> QuotaSignal:
    return QuotaSignal(
        fresh=False,
        all_accounts_exhausted=False,
        weekly_utilization=0.0,
        short_utilization=0.0,
        seconds_to_weekly_reset=None,
    )


@contextmanager
def _signals(*, load1: float = 1.0, quota: QuotaSignal | None = None) -> Iterator[None]:
    machine = MachineSignal(cores=_CORES, load1=load1, ram_available_gb=20.0)
    with ExitStack() as stack:
        stack.enter_context(patch.object(gate_mod, "governor_enabled", return_value=True))
        stack.enter_context(patch.object(gate_mod, "read_quota_signal", return_value=quota or _healthy_quota()))
        stack.enter_context(patch.object(gate_mod, "read_machine_signal", return_value=machine))
        yield


class TestDispatchAdmissionDeniedReason(TestCase):
    def test_kill_switch_off_admits(self) -> None:
        # Acceptance #3: admission_governor_enabled = false ⇒ exactly today's behaviour.
        with patch.object(gate_mod, "governor_enabled", return_value=False):
            assert dispatch_admission_denied_reason() is None

    def test_kill_switch_off_never_probes(self) -> None:
        with (
            patch.object(gate_mod, "governor_enabled", return_value=False),
            patch.object(gate_mod, "decide_admission") as decide,
        ):
            dispatch_admission_denied_reason()
        decide.assert_not_called()

    def test_the_dispatch_path_calls_decide_admission(self) -> None:
        # Acceptance #2: the governor's caller count is no longer "factory lanes only".
        with _signals(), patch.object(gate_mod, "decide_admission", wraps=gate_mod.decide_admission) as decide:
            dispatch_admission_denied_reason()
        decide.assert_called_once()

    def test_load_over_the_watermark_denies_naming_the_brake(self) -> None:
        # Acceptance #1: over the load brake watermark ⇒ denied, message names the brake.
        with _signals(load1=_OVER_THE_WATERMARK):
            reason = dispatch_admission_denied_reason()
        assert reason is not None
        assert "load" in reason
        assert "watermark" in reason

    def test_healthy_signals_below_ceiling_admit(self) -> None:
        with _signals():
            assert dispatch_admission_denied_reason() is None

    def test_a_signal_read_failure_admits_fail_open(self) -> None:
        with (
            patch.object(gate_mod, "governor_enabled", return_value=True),
            patch.object(gate_mod, "read_quota_signal", side_effect=RuntimeError("probe down")),
        ):
            assert dispatch_admission_denied_reason() is None

    def test_live_agent_count_at_ceiling_denies(self) -> None:
        with _signals(), patch.object(gate_mod, "live_agent_count", return_value=999):
            reason = dispatch_admission_denied_reason()
        assert reason is not None
        assert "at/over governor ceiling" in reason

    def test_ceiling_is_skipped_when_not_applied(self) -> None:
        # An already-admitted caller (a sub-agent whose own lane admitted it) must not be
        # re-clamped by the same ceiling — that would deadlock it against its own claim.
        with _signals(), patch.object(gate_mod, "live_agent_count", return_value=999):
            assert dispatch_admission_denied_reason(apply_ceiling=False) is None

    def test_brakes_still_apply_when_the_ceiling_is_skipped(self) -> None:
        with _signals(load1=_OVER_THE_WATERMARK):
            assert dispatch_admission_denied_reason(apply_ceiling=False) is not None

    def test_an_unknown_quota_still_bounds_the_lane(self) -> None:
        # #4097: an unknown budget is the CONSERVATIVE case, never the unbounded one — the
        # ceiling falls back to the machine signal the governor DID read, so this lane is
        # gated on a real count rather than waved through.
        with _signals(quota=_unknown_quota()), patch.object(gate_mod, "live_agent_count", return_value=999):
            reason = dispatch_admission_denied_reason()
        assert reason is not None
        assert "at/over governor ceiling" in reason

    def test_an_unknown_quota_admits_under_the_machine_ceiling(self) -> None:
        with _signals(quota=_unknown_quota()), patch.object(gate_mod, "live_agent_count", return_value=0):
            assert dispatch_admission_denied_reason() is None


class TestLiveAgentCount(TestCase):
    """The count the ceiling is compared against — the TOTAL live agent population."""

    def _claimed(self, target: str) -> Task:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket)
        return Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=target,
            status=Task.Status.CLAIMED,
            lease_expires_at=timezone.now() + dt.timedelta(minutes=10),
            phase="architectural_review",
        )

    def test_counts_both_execution_targets(self) -> None:
        # ``live_headless_agent_count`` counts only the headless half; an interactive
        # dispatch adds to the host population BOTH halves live in.
        self._claimed(Task.ExecutionTarget.HEADLESS)
        self._claimed(Task.ExecutionTarget.INTERACTIVE)
        assert gate_mod.live_agent_count() == 2

    def test_an_expired_lease_is_not_live(self) -> None:
        task = self._claimed(Task.ExecutionTarget.INTERACTIVE)
        task.lease_expires_at = timezone.now() - dt.timedelta(minutes=1)
        task.save(update_fields=["lease_expires_at"])
        assert gate_mod.live_agent_count() == 0
