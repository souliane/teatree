"""Governor gating of the HEADLESS admission chokepoints (#3644 / F9, #4098).

The pure decision (``decide_admission``) is exercised by
``test_admission_governor``; these cover the CORE-side wiring that F9 added:
the ``headless_admission_denied_reason`` seam and its consultation at the
post_save auto-enqueue and the drain safety net, so a governor DENY refuses a
new headless admission with a VISIBLE log instead of silently admitting into
the measured congestion collapse.

#4098 splits that one verdict per phase COST CLASS. The drain applied a single
verdict to the whole queue, so a 3-minute read-only ``reviewing`` task was refused
on the same brake as a 272-turn ``coding`` agent — and reviewing/shipping are what
RETIRE work, so the brake removed its own relief and held itself on for 3h22m.
``TestTheDrainDoesNotStarveTheCheapClass`` is the guard that the self-clearing
property is pinned by a test rather than by argument.
"""

from unittest.mock import MagicMock, patch

import pytest
from django.test import TestCase, override_settings

from teatree.core import headless_admission as gate_mod
from teatree.core.admission_governor import MachineSignal, QuotaSignal
from teatree.core.headless_admission import (
    HeadlessAdmission,
    headless_admission_denied_reason,
    headless_admission_verdict,
)
from teatree.core.modelkit.phases import PhaseCost
from teatree.core.models import ConfigSetting, Session, Task, Ticket

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

IMMEDIATE_BACKEND = {"TASKS": {"default": {"BACKEND": "django_tasks.backends.immediate.ImmediateBackend"}}}

_WEEK = 7 * 24 * 3600


def _healthy_quota() -> QuotaSignal:
    return QuotaSignal(
        fresh=True,
        all_accounts_exhausted=False,
        weekly_utilization=0.1,
        short_utilization=0.1,
        seconds_to_weekly_reset=_WEEK * 0.5,
    )


def _exhausted_quota() -> QuotaSignal:
    return QuotaSignal(
        fresh=True,
        all_accounts_exhausted=True,
        weekly_utilization=1.0,
        short_utilization=1.0,
        seconds_to_weekly_reset=100.0,
    )


def _machine(load1: float = 1.0) -> MachineSignal:
    return MachineSignal(cores=8, load1=load1, ram_available_gb=20.0)


def _denied(reason: str) -> HeadlessAdmission:
    return HeadlessAdmission(expensive_denied=reason, cheap_denied=reason)


def _admitted() -> HeadlessAdmission:
    return HeadlessAdmission(expensive_denied=None, cheap_denied=None)


class TestHeadlessAdmissionDeniedReason(TestCase):
    def test_kill_switch_off_admits(self) -> None:
        with patch.object(gate_mod, "governor_enabled", return_value=False):
            assert headless_admission_denied_reason() is None

    def test_a_signal_read_failure_admits_fail_open(self) -> None:
        with (
            patch.object(gate_mod, "governor_enabled", return_value=True),
            patch.object(gate_mod, "read_quota_signal", side_effect=RuntimeError("probe down")),
        ):
            assert headless_admission_denied_reason() is None

    def test_quota_exhaustion_denies_with_a_reason(self) -> None:
        with (
            patch.object(gate_mod, "governor_enabled", return_value=True),
            patch.object(gate_mod, "read_quota_signal", return_value=_exhausted_quota()),
            patch.object(gate_mod, "read_machine_signal", return_value=_machine()),
        ):
            reason = headless_admission_denied_reason()
        assert reason is not None
        assert "quota-exhausted" in reason

    def test_healthy_signals_below_ceiling_admit(self) -> None:
        with (
            patch.object(gate_mod, "governor_enabled", return_value=True),
            patch.object(gate_mod, "read_quota_signal", return_value=_healthy_quota()),
            patch.object(gate_mod, "read_machine_signal", return_value=_machine()),
        ):
            assert headless_admission_denied_reason() is None

    def test_live_count_at_ceiling_denies(self) -> None:
        # A healthy quota yields a positive ceiling; a live headless-agent count
        # at/over it is the backpressure the interactive lane already had.
        with (
            patch.object(gate_mod, "governor_enabled", return_value=True),
            patch.object(gate_mod, "read_quota_signal", return_value=_healthy_quota()),
            patch.object(gate_mod, "read_machine_signal", return_value=_machine()),
            patch.object(Task.objects, "live_headless_agent_count", return_value=999),
        ):
            reason = headless_admission_denied_reason()
        assert reason is not None
        assert "at/over governor ceiling" in reason


class TestPhaseAwareVerdict(TestCase):
    """One probe, two classes — the cheap lane survives a brake the expensive class fails."""

    #: Past the 5.0-per-core deny watermark on the 8-core ``_machine`` signal.
    _MELTED = 8 * 5.0 + 1

    def _verdict(self, *, load1: float = 1.0, live: int = 0, live_cheap: int = 0) -> HeadlessAdmission:
        with (
            patch.object(gate_mod, "governor_enabled", return_value=True),
            patch.object(gate_mod, "read_quota_signal", return_value=_healthy_quota()),
            patch.object(gate_mod, "read_machine_signal", return_value=_machine(load1=load1)),
            patch.object(Task.objects, "live_headless_agent_count", return_value=live),
            patch.object(Task.objects, "live_cheap_headless_agent_count", return_value=live_cheap),
        ):
            return headless_admission_verdict()

    def test_a_load_brake_denies_the_expensive_class_and_admits_the_cheap_one(self) -> None:
        verdict = self._verdict(load1=self._MELTED)
        assert "load" in (verdict.denied_for(PhaseCost.EXPENSIVE) or "")
        assert verdict.denied_for(PhaseCost.CHEAP) is None

    def test_the_class_is_resolved_from_the_phase(self) -> None:
        verdict = self._verdict(load1=self._MELTED)
        assert verdict.denied_reason("coding") is not None
        assert verdict.denied_reason("reviewing") is None
        # An unregistered phase inherits the braked class, never the exemption.
        assert verdict.denied_reason("banana") is not None

    def test_a_shared_ceiling_hit_by_the_expensive_class_still_admits_the_cheap_one(self) -> None:
        verdict = self._verdict(live=999)
        assert "at/over governor ceiling" in (verdict.denied_for(PhaseCost.EXPENSIVE) or "")
        assert verdict.denied_for(PhaseCost.CHEAP) is None

    def test_the_cheap_lane_is_bounded_by_its_own_ceiling(self) -> None:
        # The exemption must never become a second unbounded lane (#4097's cost).
        verdict = self._verdict(load1=self._MELTED, live_cheap=2)
        assert "cheap-phase" in (verdict.denied_for(PhaseCost.CHEAP) or "")

    def test_a_spent_token_budget_still_denies_the_cheap_class(self) -> None:
        # The exemption is from the MACHINE brake only. A review burns tokens like
        # anything else, so a spent weekly window refuses every class.
        with (
            patch.object(gate_mod, "governor_enabled", return_value=True),
            patch.object(gate_mod, "read_quota_signal", return_value=_exhausted_quota()),
            patch.object(gate_mod, "read_machine_signal", return_value=_machine()),
        ):
            verdict = headless_admission_verdict()
        assert verdict.denied_for(PhaseCost.CHEAP) is not None
        assert verdict.denied_for(PhaseCost.EXPENSIVE) is not None

    def test_a_zero_ceiling_collapses_the_cheap_class_onto_the_expensive_one(self) -> None:
        # The rollback lever: no exemption at all, byte-identical to the pre-#4098 verdict.
        ConfigSetting.objects.set_value("cheap_phase_admission_ceiling", 0)
        verdict = self._verdict(load1=self._MELTED)
        assert verdict.denied_for(PhaseCost.CHEAP) == verdict.denied_for(PhaseCost.EXPENSIVE)

    def test_the_kill_switch_admits_both_classes(self) -> None:
        with patch.object(gate_mod, "governor_enabled", return_value=False):
            verdict = headless_admission_verdict()
        assert verdict.denied_for(PhaseCost.CHEAP) is None
        assert verdict.denied_for(PhaseCost.EXPENSIVE) is None

    def test_a_probe_failure_admits_both_classes_fail_open(self) -> None:
        with (
            patch.object(gate_mod, "governor_enabled", return_value=True),
            patch.object(gate_mod, "read_quota_signal", side_effect=RuntimeError("probe down")),
        ):
            verdict = headless_admission_verdict()
        assert verdict.denied_for(PhaseCost.CHEAP) is None
        assert verdict.denied_for(PhaseCost.EXPENSIVE) is None

    def test_the_single_shot_wrapper_defaults_to_the_expensive_class(self) -> None:
        # Back-compat: the phase-less call is the pre-#4098 verdict verbatim, so a
        # caller that admits new coding work keeps braking exactly as it did.
        with (
            patch.object(gate_mod, "governor_enabled", return_value=True),
            patch.object(gate_mod, "read_quota_signal", return_value=_healthy_quota()),
            patch.object(gate_mod, "read_machine_signal", return_value=_machine(load1=self._MELTED)),
        ):
            assert headless_admission_denied_reason() is not None
            assert headless_admission_denied_reason("reviewing") is None

    def test_every_refused_class_is_announced_and_an_admitted_one_is_not(self) -> None:
        # A refusal is never silent, and the wording lives on the verdict so both
        # chokepoints report it identically.
        with self.assertLogs("teatree.core.headless_admission", level="WARNING") as logs:
            self._verdict(load1=self._MELTED).log_denials()
        assert any("expensive" in line for line in logs.output)
        assert not any("cheap" in line for line in logs.output)


class TestDrainConsultsTheGovernor(TestCase):
    def setUp(self) -> None:
        from django.db.models.signals import post_save  # noqa: PLC0415 - deferred: local import

        from teatree.core.signals import _auto_enqueue_headless_task  # noqa: PLC0415 - deferred: local import

        post_save.disconnect(_auto_enqueue_headless_task, sender=Task, dispatch_uid="auto_enqueue_headless")
        self.addCleanup(
            post_save.connect, _auto_enqueue_headless_task, sender=Task, dispatch_uid="auto_enqueue_headless"
        )

    def _pending_headless(self) -> Task:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket)
        return Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.HEADLESS,
            status=Task.Status.PENDING,
            phase="architectural_review",
        )

    @override_settings(**IMMEDIATE_BACKEND)
    def test_drain_admits_nothing_on_a_governor_deny(self) -> None:
        from teatree.core.tasks import drain_headless_queue_body  # noqa: PLC0415 - deferred: local import

        task = self._pending_headless()
        with patch.object(gate_mod, "headless_admission_verdict", return_value=_denied("weekly window spent")):
            result = drain_headless_queue_body()

        assert result["enqueued"] == []
        task.refresh_from_db()
        assert task.status == Task.Status.PENDING

    @override_settings(**IMMEDIATE_BACKEND)
    def test_drain_admits_when_the_governor_is_silent(self) -> None:
        from teatree.core.tasks import drain_headless_queue_body  # noqa: PLC0415 - deferred: local import

        task = self._pending_headless()
        with (
            patch.object(gate_mod, "headless_admission_verdict", return_value=_admitted()),
            patch("teatree.core.tasks.execute_headless_task") as enqueue_task,
        ):
            enqueue_task.enqueue = MagicMock()
            result = drain_headless_queue_body()

        assert task.pk in result["enqueued"]


class TestTheDrainDoesNotStarveTheCheapClass(TestCase):
    """#4098's headline guard: a drain that denies the expensive class still admits the cheap one.

    Measured 2026-08-03: the load brake denied EVERY headless admission for 3h22m —
    zero review verdicts, 18 pending tasks, no merges. Reviewing and shipping are what
    retire a worktree and its agent, so refusing them alongside the coding agents that
    caused the load removed the only work that would have relieved it. This exercises
    the real signal chain (not the seam) so the self-clearing property is pinned rather
    than argued.
    """

    #: Past the 5.0-per-core deny watermark on the 8-core ``_machine`` signal.
    _MELTED = 8 * 5.0 + 1

    def setUp(self) -> None:
        from django.db.models.signals import post_save  # noqa: PLC0415 - deferred: local import

        from teatree.core.signals import _auto_enqueue_headless_task  # noqa: PLC0415 - deferred: local import

        post_save.disconnect(_auto_enqueue_headless_task, sender=Task, dispatch_uid="auto_enqueue_headless")
        self.addCleanup(
            post_save.connect, _auto_enqueue_headless_task, sender=Task, dispatch_uid="auto_enqueue_headless"
        )

    def _pending(self, phase: str) -> Task:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket)
        return Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.HEADLESS,
            status=Task.Status.PENDING,
            phase=phase,
        )

    def _drain_under_load(self, load1: float) -> tuple[dict, MagicMock]:
        from teatree.core.tasks import drain_headless_queue_body  # noqa: PLC0415 - deferred: local import

        with (
            patch.object(gate_mod, "governor_enabled", return_value=True),
            patch.object(gate_mod, "read_quota_signal", return_value=_healthy_quota()) as quota_probe,
            patch.object(gate_mod, "read_machine_signal", return_value=_machine(load1=load1)),
            patch("teatree.core.tasks.execute_headless_task") as enqueue_task,
        ):
            enqueue_task.enqueue = MagicMock()
            return drain_headless_queue_body(), quota_probe

    @override_settings(**IMMEDIATE_BACKEND)
    def test_a_braked_drain_admits_the_cheap_class_and_holds_the_expensive_one(self) -> None:
        coding = self._pending("coding")
        reviewing = self._pending("reviewing")

        result, _ = self._drain_under_load(self._MELTED)

        assert result["enqueued"] == [reviewing.pk]
        coding.refresh_from_db()
        assert coding.status == Task.Status.PENDING

    @override_settings(**IMMEDIATE_BACKEND)
    def test_an_unbraked_drain_admits_both_classes(self) -> None:
        coding = self._pending("coding")
        reviewing = self._pending("reviewing")

        result, _ = self._drain_under_load(load1=1.0)

        assert set(result["enqueued"]) == {coding.pk, reviewing.pk}

    @override_settings(**IMMEDIATE_BACKEND)
    def test_the_governor_is_probed_once_per_drain_not_once_per_row(self) -> None:
        # Nothing changes between iterations of the drain loop, so a per-task re-ask
        # would return the same verdict N times and cost N probes. The verdict is
        # computed once and RESOLVED per row.
        for phase in ("coding", "reviewing", "shipping"):
            self._pending(phase)

        _, quota_probe = self._drain_under_load(self._MELTED)

        assert quota_probe.call_count == 1

    @override_settings(**IMMEDIATE_BACKEND)
    def test_the_cheap_lane_bound_still_holds_inside_the_drain(self) -> None:
        # The exemption is bounded, so a full cheap lane holds cheap rows too — it can
        # never become the second unbounded lane.
        reviewing = self._pending("reviewing")
        with patch.object(Task.objects, "live_cheap_headless_agent_count", return_value=2):
            result, _ = self._drain_under_load(self._MELTED)

        assert result["enqueued"] == []
        reviewing.refresh_from_db()
        assert reviewing.status == Task.Status.PENDING

    @override_settings(**IMMEDIATE_BACKEND)
    def test_the_cheap_lane_bound_binds_within_one_drain(self) -> None:
        # The lane's ceiling counts CLAIMED agents and a row this drain enqueues is
        # still PENDING, so a re-probe cannot see it. Without the per-pass headroom a
        # single braked drain would admit every pending cheap row at once — the second
        # unbounded lane the exemption must never become.
        pending = [self._pending("reviewing") for _ in range(5)]
        with patch.object(Task.objects, "live_cheap_headless_agent_count", return_value=1):
            result, _ = self._drain_under_load(self._MELTED)

        # ceiling 2 minus 1 live cheap agent leaves headroom for exactly one more.
        assert result["enqueued"] == [pending[0].pk]


class TestAutoEnqueueConsultsTheGovernor(TestCase):
    @override_settings(**IMMEDIATE_BACKEND)
    def test_auto_enqueue_is_suppressed_on_a_governor_deny(self) -> None:
        # The post_save auto-enqueue must consult the governor: a DENY leaves the
        # task PENDING for the (also-gated) drain, never fires the dispatch.
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket)
        with (
            patch.object(gate_mod, "headless_admission_denied_reason", return_value="load over watermark"),
            patch("teatree.core.tasks.execute_headless_task") as enqueue_task,
        ):
            enqueue_task.enqueue = MagicMock()
            Task.objects.create(
                ticket=ticket,
                session=session,
                execution_target=Task.ExecutionTarget.HEADLESS,
                status=Task.Status.PENDING,
                phase="architectural_review",
            )
            enqueue_task.enqueue.assert_not_called()

    def _create_pending(self, phase: str) -> None:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket)
        Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.HEADLESS,
            status=Task.Status.PENDING,
            phase=phase,
        )

    @override_settings(**IMMEDIATE_BACKEND)
    def test_auto_enqueue_classifies_by_phase_like_the_drain(self) -> None:
        # The two chokepoints share one classification, so they cannot diverge on which
        # rows a braked box still admits.
        with (
            patch.object(gate_mod, "governor_enabled", return_value=True),
            patch.object(gate_mod, "read_quota_signal", return_value=_healthy_quota()),
            patch.object(gate_mod, "read_machine_signal", return_value=_machine(load1=8 * 5.0 + 1)),
            patch("teatree.core.tasks.execute_headless_task") as enqueue_task,
        ):
            enqueue_task.enqueue = MagicMock()
            self._create_pending("coding")
            enqueue_task.enqueue.assert_not_called()
            self._create_pending("reviewing")
            enqueue_task.enqueue.assert_called_once()
