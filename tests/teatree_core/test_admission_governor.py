"""The adaptive admission governor (#3644).

Token budget is the PRIMARY signal and machine pressure the secondary one, so the
matrix below always states both: an idle box with no weekly quota admits NOTHING,
and a healthy weekly window still yields to a melting box. Every refusal carries a
reason — a governor that denies silently recreates the class of bug that hid a dead
merge loop for weeks.
"""

import pytest

from teatree.agents import _headless_env
from teatree.agents._headless_env import XDIST_WORKERS_VAR, with_test_worker_cap
from teatree.core import admission_governor
from teatree.core.admission_governor import (
    MachineSignal,
    QuotaSignal,
    YieldSignal,
    decide_admission,
    per_agent_test_workers,
    read_machine_signal,
    weekly_pace,
)

_WEEK = 7 * 24 * 3600


def _quota(**kwargs: object) -> QuotaSignal:
    base: dict[str, object] = {
        "fresh": True,
        "all_accounts_exhausted": False,
        "weekly_utilization": 0.1,
        "short_utilization": 0.1,
        "seconds_to_weekly_reset": _WEEK * 0.5,
    }
    return QuotaSignal(**{**base, **kwargs})


def _machine(**kwargs: object) -> MachineSignal:
    base: dict[str, object] = {"cores": 8, "load1": 1.0, "ram_available_gb": 20.0}
    return MachineSignal(**{**base, **kwargs})


def _decide(*, quota: QuotaSignal | None = None, machine: MachineSignal | None = None, **kwargs: object):
    return decide_admission(quota=quota or _quota(), machine=machine or _machine(), **kwargs)


class TestTokenBudgetIsPrimary:
    def test_idle_box_with_no_weekly_quota_admits_nothing(self) -> None:
        decision = _decide(quota=_quota(weekly_utilization=0.999), machine=_machine(load1=0.0))
        assert not decision.admit
        assert "weekly" in decision.reason

    def test_every_account_exhausted_is_a_hard_brake(self) -> None:
        decision = _decide(quota=_quota(all_accounts_exhausted=True), machine=_machine(load1=0.0))
        assert not decision.admit
        assert "exhausted" in decision.reason

    def test_short_window_exhaustion_brakes_even_with_weekly_headroom(self) -> None:
        decision = _decide(quota=_quota(short_utilization=0.99, weekly_utilization=0.05))
        assert not decision.admit

    def test_burn_ahead_of_runway_tightens_the_ceiling_without_denying(self) -> None:
        paced = _decide(quota=_quota(weekly_utilization=0.8, seconds_to_weekly_reset=_WEEK * 0.5))
        roomy = _decide(quota=_quota(weekly_utilization=0.1, seconds_to_weekly_reset=_WEEK * 0.5))
        assert paced.admit
        assert paced.ceiling < roomy.ceiling

    def test_idle_box_with_healthy_quota_raises_toward_the_ceiling(self) -> None:
        decision = _decide()
        assert decision.admit
        assert decision.ceiling >= 2  # the empirical 8-core WRITE default


class TestMachinePressureIsSecondary:
    def test_load_above_the_brake_denies_while_quota_is_healthy(self) -> None:
        decision = _decide(machine=_machine(load1=8 * 5.0 + 1))
        assert not decision.admit
        assert "load" in decision.reason

    def test_load_between_the_watermarks_holds_a_braked_governor_braked(self) -> None:
        mid = _machine(load1=8 * 4.0)
        assert not _decide(machine=mid, braked=True).admit
        assert _decide(machine=mid, braked=False).admit

    def test_falling_below_the_low_watermark_re_admits_a_braked_governor(self) -> None:
        assert _decide(machine=_machine(load1=8 * 1.0), braked=True).admit


class TestYieldPerToken:
    def test_collapsed_yield_stops_admitting_rather_than_throttling(self) -> None:
        decision = _decide(yield_signal=YieldSignal(completed=0, failed=12))
        assert not decision.admit
        assert "yield" in decision.reason

    def test_unknown_yield_never_brakes(self) -> None:
        assert _decide(yield_signal=YieldSignal(completed=0, failed=0)).admit

    def test_healthy_yield_never_brakes(self) -> None:
        assert _decide(yield_signal=YieldSignal(completed=9, failed=1)).admit


class TestFailSafeAndFloor:
    def test_an_unreadable_quota_probe_admits_without_tightening(self) -> None:
        # CORRECTED contract (see TestUnreadableProbeNeverManufacturesAClamp): the
        # first cut asserted a clamp-down to 1 here, which was the defect itself.
        decision = _decide(quota=_quota(fresh=False), static_ceiling=6)
        assert decision.admit
        assert decision.ceiling == 6

    def test_the_ceiling_never_deadlocks_the_factory_to_zero(self) -> None:
        decision = _decide(
            quota=_quota(weekly_utilization=0.9, seconds_to_weekly_reset=_WEEK),
            machine=_machine(cores=1, load1=1.0),
        )
        assert decision.ceiling >= 1

    def test_a_static_setting_is_a_ceiling_not_a_target(self) -> None:
        capped = _decide(static_ceiling=1)
        assert capped.ceiling == 1
        assert _decide(static_ceiling=1000).ceiling < 1000

    def test_every_decision_carries_a_reason(self) -> None:
        assert _decide().reason


class TestTestWorkerBudget:
    def test_total_workers_stay_bounded_across_the_governable_agent_range(self) -> None:
        # Up to the widest count the admission ceiling can ever produce, the TOTAL holds.
        for agents in range(1, 8 * 2 + 1):
            assert per_agent_test_workers(cores=8, active_agents=agents) * agents <= 8 * 2

    def test_the_measured_meltdown_arithmetic_can_no_longer_happen(self) -> None:
        # 12 implementers x auto-detected 8 workers produced ~96 workers, load ~70.
        assert per_agent_test_workers(cores=8, active_agents=12) * 12 < 96

    def test_a_lone_agent_still_gets_real_parallelism(self) -> None:
        assert per_agent_test_workers(cores=8, active_agents=1) > 1

    def test_never_drops_below_one_worker(self) -> None:
        # Past the governable range the floor wins over the total bound: an agent with
        # zero test workers cannot run its suite at all.
        assert per_agent_test_workers(cores=8, active_agents=1000) == 1

    @pytest.mark.parametrize("agents", [0, -1])
    def test_a_nonsense_agent_count_is_treated_as_one(self, agents: int) -> None:
        assert per_agent_test_workers(cores=8, active_agents=agents) == per_agent_test_workers(cores=8, active_agents=1)


class TestTestWorkerCapWiring:
    """#3644: the cap reaches the child agent's env, and the kill-switch removes it."""

    def test_cap_is_merged_onto_a_pinned_credential_env(self) -> None:
        capped = with_test_worker_cap({"ANTHROPIC_API_KEY": "x"}, active_agents=4)
        assert capped is not None
        assert capped["ANTHROPIC_API_KEY"] == "x"
        assert int(capped[XDIST_WORKERS_VAR]) >= 1

    def test_cap_applies_even_when_the_child_inherits_the_ambient_env(self) -> None:
        capped = with_test_worker_cap(None, active_agents=4)
        assert capped is not None
        assert set(capped) == {XDIST_WORKERS_VAR}

    def test_kill_switch_removes_the_cap_entirely(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(admission_governor, "governor_enabled", lambda: False)
        assert _headless_env.with_test_worker_cap(None, active_agents=4) is None
        assert _headless_env.with_test_worker_cap({"A": "b"}, active_agents=4) == {"A": "b"}


class TestUnreadableProbeNeverManufacturesAClamp:
    """#3644 regression: a probe that cannot read must not TIGHTEN admission.

    The first cut treated "quota unreadable" as a reason to clamp the ceiling to 1.
    That is the silent-starvation failure the governor exists to prevent: the quota
    cache is cold on every fresh install and permanently cold for an operator who
    pins no subscription account, so the governor pinned concurrency to 1 forever on
    evidence it never had — and, worse, manufactured a clamp where the operator's own
    state said UNCLAMPED. Conservative means "do not RAISE", never "clamp down".
    """

    def test_an_unreadable_probe_leaves_an_absent_static_ceiling_unclamped(self) -> None:
        assert _decide(quota=_quota(fresh=False), static_ceiling=None).ceiling is None

    def test_an_unreadable_probe_preserves_the_operators_static_ceiling(self) -> None:
        assert _decide(quota=_quota(fresh=False), static_ceiling=4).ceiling == 4

    def test_an_unreadable_probe_still_admits(self) -> None:
        assert _decide(quota=_quota(fresh=False), static_ceiling=None).admit

    def test_a_fresh_probe_may_still_clamp_below_the_static_ceiling(self) -> None:
        # Tightening is legitimate when the governor actually HAS the evidence.
        tightened = _decide(
            quota=_quota(weekly_utilization=0.95, seconds_to_weekly_reset=_WEEK),
            machine=_machine(cores=8),
            static_ceiling=8,
        )
        assert tightened.ceiling is not None
        assert tightened.ceiling < 8

    def test_a_machine_brake_still_denies_even_when_the_quota_probe_is_unreadable(self) -> None:
        # The two signals are independent: an unreadable token probe must not disarm
        # the load brake, which reads its own signal successfully.
        denied = _decide(quota=_quota(fresh=False), machine=_machine(load1=8 * 5.0 + 1))
        assert not denied.admit


class TestWeeklyPace:
    def test_on_pace_is_one(self) -> None:
        # Half the weekly window spent with half the runway left is exactly on pace.
        assert weekly_pace(_quota(weekly_utilization=0.5, seconds_to_weekly_reset=_WEEK * 0.5)) == pytest.approx(1.0)

    def test_underspent_window_paces_above_one(self) -> None:
        assert weekly_pace(_quota(weekly_utilization=0.1, seconds_to_weekly_reset=_WEEK * 0.5)) > 1.0

    def test_a_nonpositive_runway_reads_as_fully_on_pace(self) -> None:
        # A reset that is due now (or already past) makes the runway zero; dividing by it
        # would blow up, so the guard returns 1.0 — no pacing pressure from a spent clock.
        assert weekly_pace(_quota(weekly_utilization=0.4, seconds_to_weekly_reset=0.0)) == pytest.approx(1.0)
        assert weekly_pace(_quota(weekly_utilization=0.4, seconds_to_weekly_reset=-500.0)) == pytest.approx(1.0)


class TestReadMachineSignal:
    def test_reads_the_live_load_and_cores(self) -> None:
        signal = read_machine_signal()
        assert signal.cores >= 1
        assert signal.load1 >= 0.0

    def test_an_unreadable_loadavg_degrades_to_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A platform without getloadavg (or a probe error) must not crash the governor:
        # the load reads 0.0, so the machine brake simply never fires on this box.
        def _boom() -> tuple[float, float, float]:
            msg = "no loadavg on this platform"
            raise OSError(msg)

        monkeypatch.setattr(admission_governor.os, "getloadavg", _boom)
        signal = read_machine_signal()
        assert signal.load1 == pytest.approx(0.0)
        assert signal.cores >= 1
