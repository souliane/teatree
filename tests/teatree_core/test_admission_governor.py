"""The adaptive admission governor (#3644).

Token budget is the PRIMARY signal and machine pressure the secondary one, so the
matrix below always states both: an idle box with no weekly quota admits NOTHING,
and a healthy weekly window still yields to a melting box. Every refusal carries a
reason — a governor that denies silently recreates the class of bug that hid a dead
merge loop for weeks.
"""

import datetime as dt
import math

import pytest
from django.test import TestCase
from django.utils import timezone

from teatree.agents import _runner_env
from teatree.agents._runner_env import XDIST_WORKERS_VAR, with_test_worker_cap
from teatree.core import admission_governor
from teatree.core.admission_governor import (
    RAM_BRAKE_FLOOR_GB,
    RAM_RESUME_FLOOR_GB,
    MachineBrake,
    MachineSignal,
    MergeSignal,
    QuotaSignal,
    YieldSignal,
    decide_admission,
    per_agent_test_workers,
    read_machine_signal,
    resume_agent_ceiling,
    resume_shed_directive,
    weekly_pace,
)
from teatree.core.models.anthropic_token_usage import AnthropicTokenUsage
from teatree.utils import ram_scope
from teatree.utils.ram_scope import RamHeadroom

_WEEK = 7 * 24 * 3600

#: The measured p90 pytest-xdist worker RSS the ``test_worker_ram_gb`` default ships at.
_P90_WORKER_GB = 1.25


def _quota(**kwargs: object) -> QuotaSignal:
    base: dict[str, object] = {
        "fresh": True,
        "all_accounts_exhausted": False,
        "weekly_utilization": 0.1,
        "short_utilization": 0.1,
        "seconds_to_weekly_reset": _WEEK * 0.5,
    }
    return QuotaSignal(**{**base, **kwargs})


def _stub_headroom(
    monkeypatch: pytest.MonkeyPatch,
    *,
    available_mib: int | None,
    cgroup_limit_mib: int | None = None,
    host_available_mib: int | None = None,
) -> None:
    """Stand the governor's probe in a named scope — uncapped, and on a roomy box, by default."""
    headroom = RamHeadroom(
        available_mib=available_mib,
        cgroup_limit_mib=cgroup_limit_mib,
        host_available_mib=available_mib if host_available_mib is None else host_available_mib,
    )
    monkeypatch.setattr(ram_scope, "read_ram_headroom", lambda: headroom)


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
        assert not _decide(machine=mid, load_brake=MachineBrake(braked=True)).admit
        assert _decide(machine=mid, load_brake=MachineBrake(braked=False)).admit

    def test_falling_below_the_low_watermark_re_admits_a_braked_governor(self) -> None:
        assert _decide(machine=_machine(load1=8 * 1.0), load_brake=MachineBrake(braked=True)).admit


class TestMachineBrakeExemption:
    """``load_brake=MachineBrake(applies=False)`` — the cheap class's bounded exemption (#4098).

    The cheap read-only phases are what DRAIN the box, so the load brake that the
    expensive class caused must not also refuse them. The exemption is from the LOAD
    brake ONLY: a spent token budget still refuses everything, cheap included.
    """

    _MELTED = 8 * 5.0 + 1

    def test_the_default_is_todays_behaviour(self) -> None:
        assert not _decide(machine=_machine(load1=self._MELTED)).admit

    def test_the_exemption_admits_through_a_load_brake(self) -> None:
        decision = _decide(machine=_machine(load1=self._MELTED), load_brake=MachineBrake(applies=False))
        assert decision.admit
        assert not decision.braked

    def test_the_exemption_does_not_survive_a_spent_quota(self) -> None:
        decision = _decide(
            quota=_quota(weekly_utilization=0.999),
            machine=_machine(load1=0.0),
            load_brake=MachineBrake(applies=False),
        )
        assert not decision.admit
        assert "weekly" in decision.reason

    def test_the_exemption_does_not_survive_exhausted_accounts(self) -> None:
        decision = _decide(
            quota=_quota(all_accounts_exhausted=True),
            machine=_machine(load1=self._MELTED),
            load_brake=MachineBrake(applies=False),
        )
        assert not decision.admit
        assert "exhausted" in decision.reason

    def test_the_exemption_leaves_the_ceiling_untouched(self) -> None:
        quiet = _machine(load1=0.0)
        assert _decide(machine=quiet, load_brake=MachineBrake(applies=False)).ceiling == _decide(machine=quiet).ceiling


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
    def test_an_unreadable_quota_probe_still_admits(self) -> None:
        # An unreadable probe bounds the lane (see TestAnUnknownQuotaIsBoundedNotUnlimited)
        # but never DENIES: the governor has no evidence of a spent budget.
        decision = _decide(quota=_quota(fresh=False), static_ceiling=6)
        assert decision.admit
        assert decision.ceiling == 4

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
        assert _runner_env.with_test_worker_cap(None, active_agents=4) is None
        assert _runner_env.with_test_worker_cap({"A": "b"}, active_agents=4) == {"A": "b"}


class TestTheExportedCapRespondsToMemory:
    """#4163: the seam that reaches the child agent — the cap MOVES with the reading.

    The governor bounded workers on cores alone, so 16 xdist workers at the measured p90
    RSS of 1.24 GB each is 19.8 GB against 19.7 GB usable — the whole story of 1374 OOM
    kills in a fortnight. This is the behavioural end of the fix: the same call on a
    40 GB box and on a 6 GB one must not hand the child the same number.
    """

    def _exported_workers(self, monkeypatch: pytest.MonkeyPatch, *, available_gb: float) -> int:
        _stub_headroom(monkeypatch, available_mib=round(available_gb * 1024))
        capped = with_test_worker_cap(None, active_agents=1)
        assert capped is not None
        return int(capped[XDIST_WORKERS_VAR])

    def test_a_memory_tight_box_exports_fewer_workers_than_a_roomy_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        roomy = self._exported_workers(monkeypatch, available_gb=40.0)
        tight = self._exported_workers(monkeypatch, available_gb=6.0)
        assert tight < roomy


class TestTheWorkerBoundReadsMemory:
    """#4163: the pure bound, with the reading and the per-worker size passed IN.

    The pure function never reads config — the consumer resolves ``test_worker_ram_gb``
    and hands it over, so the arithmetic stays testable without a settings store.
    """

    def test_the_bound_falls_with_the_reading(self) -> None:
        roomy = per_agent_test_workers(cores=8, active_agents=1, ram_available_gb=40.0, per_worker_gb=_P90_WORKER_GB)
        tight = per_agent_test_workers(cores=8, active_agents=1, ram_available_gb=6.0, per_worker_gb=_P90_WORKER_GB)
        assert tight < roomy

    def test_an_unread_reading_returns_exactly_the_cpu_bound(self) -> None:
        # #4101 restated: an unreadable probe falls back to the bound derived from the
        # signal that WAS read — never a manufactured clamp to 1, and never 0.
        assert per_agent_test_workers(
            cores=8, active_agents=4, ram_available_gb=None, per_worker_gb=_P90_WORKER_GB
        ) == per_agent_test_workers(cores=8, active_agents=4)
        assert per_agent_test_workers(cores=8, active_agents=4, ram_available_gb=None) == 4

    def test_an_unsized_worker_returns_exactly_the_cpu_bound(self) -> None:
        # No per-worker size is the same missing evidence as no reading.
        assert per_agent_test_workers(cores=8, active_agents=4, ram_available_gb=6.0, per_worker_gb=0.0) == 4

    def test_the_total_stays_within_what_memory_pays_for_across_the_agent_range(self) -> None:
        budget = math.floor((20.0 - RAM_BRAKE_FLOOR_GB) / _P90_WORKER_GB)
        for agents in range(1, budget + 1):
            workers = per_agent_test_workers(
                cores=8, active_agents=agents, ram_available_gb=20.0, per_worker_gb=_P90_WORKER_GB
            )
            assert workers * agents <= budget

    def test_a_starved_box_still_gets_one_worker(self) -> None:
        # The floor wins over the memory bound for the same reason it wins over the
        # total bound: an agent with zero test workers cannot run its suite at all.
        assert per_agent_test_workers(cores=8, active_agents=4, ram_available_gb=1.0, per_worker_gb=_P90_WORKER_GB) == 1


class TestTheMemoryBrake:
    """A RAM watermark beside the load one — same hysteresis, same cheap-lane exemption."""

    def test_a_reading_under_the_floor_denies_and_names_the_reading(self) -> None:
        decision = _decide(machine=_machine(ram_available_gb=3.0))
        assert not decision.admit
        assert "3.0 GB" in decision.reason

    def test_an_unread_reading_admits(self) -> None:
        # Fail-safe is BOUNDED, not closed: denying on an unreadable /proc file is a
        # kill switch operated by a missing file (#4097).
        assert _decide(machine=_machine(ram_available_gb=None)).admit

    def test_a_braked_governor_is_held_to_the_higher_floor(self) -> None:
        between = _machine(ram_available_gb=(RAM_BRAKE_FLOOR_GB + RAM_RESUME_FLOOR_GB) / 2)
        assert _decide(machine=between).admit
        assert not _decide(machine=between, load_brake=MachineBrake(braked=True)).admit

    def test_the_cheap_lane_skips_the_memory_brake_as_it_skips_load(self) -> None:
        starved = _machine(ram_available_gb=1.0)
        assert not _decide(machine=starved).admit
        assert _decide(machine=starved, load_brake=MachineBrake(applies=False)).admit


class TestResumeCeilingReadsMemory:
    """#4108's restore bound, now ``min(load headroom, memory headroom)``."""

    def test_a_memory_tight_box_carries_fewer_agents(self) -> None:
        idle = _machine(cores=8, load1=0.0)
        roomy = resume_agent_ceiling(idle)
        tight = resume_agent_ceiling(_machine(cores=8, load1=0.0, ram_available_gb=5.0))
        assert 1 <= tight < roomy

    def test_an_unread_reading_leaves_the_ceiling_load_derived(self) -> None:
        assert resume_agent_ceiling(_machine(cores=8, load1=0.0, ram_available_gb=None)) == 8

    def test_the_recorded_meltdown_reading_leaves_room_for_one(self) -> None:
        # Load 58 on 8 cores with 1 GB free — the reading taken during the restore.
        assert resume_agent_ceiling(_machine(cores=8, load1=58.0, ram_available_gb=1.0)) == 1

    def test_memory_alone_never_takes_the_ceiling_to_zero(self) -> None:
        assert resume_agent_ceiling(_machine(cores=8, load1=0.0, ram_available_gb=0.0)) == 1


class TestReadMachineSignalPopulatesMemory:
    """The field was declared and populated by nothing — every live caller passed no argument."""

    def test_the_default_path_reads_the_cgroup_aware_probe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub_headroom(monkeypatch, available_mib=8 * 1024)
        assert read_machine_signal().ram_available_gb == pytest.approx(8.0)

    def test_an_unreadable_probe_reports_none_rather_than_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # "Unreadable" and "no headroom left" are different answers and must not collapse.
        _stub_headroom(monkeypatch, available_mib=None)
        assert read_machine_signal().ram_available_gb is None

    def test_an_explicit_reading_is_never_overwritten(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub_headroom(monkeypatch, available_mib=8 * 1024)
        assert read_machine_signal(ram_available_gb=12.5).ram_available_gb == pytest.approx(12.5)


class TestTwoContainersOneBox:
    """#4217 — the same code, the same instant, two cgroups, one verdict.

    The admin sidecar read 1.65 GB while the worker read 15.88 GB with 22 GB free on the
    box, so every dispatch made from the container interactive work runs in was denied
    against an absolute floor its fixed 2 GiB cap could never rise above. The deny text
    told the operator to wait for hysteresis that can never arrive.
    """

    _ADMIN_CAP_MIB = 2 * 1024
    _WORKER_CAP_MIB = 22155
    _HOST_FREE_MIB = 22557

    def _refusal(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        available_mib: int,
        cgroup_limit_mib: int,
        host_available_mib: int = _HOST_FREE_MIB,
    ) -> str:
        """The reason a dispatch is refused in that cgroup — empty when it is admitted.

        Load is pinned rather than read live so the assertion is about memory scope alone.
        """
        _stub_headroom(
            monkeypatch,
            available_mib=available_mib,
            cgroup_limit_mib=cgroup_limit_mib,
            host_available_mib=host_available_mib,
        )
        ram_available_gb = read_machine_signal().ram_available_gb
        decision = _decide(machine=_machine(load1=1.0, ram_available_gb=ram_available_gb))
        return "" if decision.admit else decision.reason

    def test_the_sidecars_reading_never_brakes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert self._refusal(monkeypatch, available_mib=1691, cgroup_limit_mib=self._ADMIN_CAP_MIB) == ""

    def test_the_workers_reading_still_brakes_when_genuinely_low(self, monkeypatch: pytest.MonkeyPatch) -> None:
        low = round(RAM_BRAKE_FLOOR_GB * 1024) - 1
        assert "watermark" in self._refusal(
            monkeypatch, available_mib=low, cgroup_limit_mib=self._WORKER_CAP_MIB, host_available_mib=low
        )

    def test_the_two_containers_no_longer_disagree(self, monkeypatch: pytest.MonkeyPatch) -> None:
        admin = self._refusal(monkeypatch, available_mib=1691, cgroup_limit_mib=self._ADMIN_CAP_MIB)
        worker = self._refusal(monkeypatch, available_mib=16261, cgroup_limit_mib=self._WORKER_CAP_MIB)
        assert admin == worker == ""

    def test_a_starved_box_brakes_from_the_sidecar_too(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # #4252: the arm the abstention left open. Same 2 GiB sidecar, but the BOX is out
        # of memory — the reading it holds is box-scoped and must reach the watermark.
        starved = round(RAM_BRAKE_FLOOR_GB * 1024) - 1
        assert "watermark" in self._refusal(
            monkeypatch, available_mib=starved, cgroup_limit_mib=self._ADMIN_CAP_MIB, host_available_mib=starved
        )


class TestUnreadableProbeNeverManufacturesAClamp:
    """#3644 regression: a probe that cannot read must not INVENT a tighter ceiling.

    The first cut treated "quota unreadable" as a reason to clamp the ceiling to 1.
    That is the silent-starvation failure the governor exists to prevent: the quota
    cache is cold on every fresh install and permanently cold for an operator who
    pins no subscription account, so the governor pinned concurrency to 1 forever on
    evidence it never had. The bound an unknown quota falls back to is the one derived
    from the machine signal it DID read (#4097) — never a constant, and never below
    what the same box would get on a healthy quota.
    """

    def test_an_unreadable_probe_never_tightens_below_the_healthy_ceiling(self) -> None:
        assert _decide(quota=_quota(fresh=False), static_ceiling=None).ceiling == _decide().ceiling

    def test_an_unreadable_probe_preserves_the_operators_static_ceiling(self) -> None:
        # 32 cores so the machine ceiling is 16: a static 4 that merely COINCIDED with
        # the machine answer would pass whether or not it was honoured.
        assert _decide(quota=_quota(fresh=False), machine=_machine(cores=32), static_ceiling=4).ceiling == 4

    def test_an_unreadable_probe_still_admits(self) -> None:
        assert _decide(quota=_quota(fresh=False), static_ceiling=None).admit

    def test_a_fresh_probe_may_still_clamp_below_the_static_ceiling(self) -> None:
        # Tightening is legitimate when the governor actually HAS the evidence.
        tightened = _decide(
            quota=_quota(weekly_utilization=0.95, seconds_to_weekly_reset=_WEEK),
            machine=_machine(cores=8),
            static_ceiling=8,
        )
        assert tightened.ceiling < 8

    def test_a_machine_brake_still_denies_even_when_the_quota_probe_is_unreadable(self) -> None:
        # The two signals are independent: an unreadable token probe must not disarm
        # the load brake, which reads its own signal successfully.
        denied = _decide(quota=_quota(fresh=False), machine=_machine(load1=8 * 5.0 + 1))
        assert not denied.admit


class TestAnUnknownQuotaIsBoundedNotUnlimited:
    """#4097: not knowing the budget must never buy MORE concurrency than knowing it.

    An unknown quota used to leave ``static_ceiling`` verbatim, so the agent lane —
    which passes ``static_ceiling=None`` — got no ceiling at all, while a known-healthy
    quota got ``floor(cores * WRITE_CONCURRENCY_PER_CORE)``. The machine-derived base
    needs no quota information whatsoever, so it is available in both cases; only the
    weekly-pace scaling on top of it genuinely requires a fresh quota.
    """

    def test_an_unknown_quota_still_yields_a_bounded_ceiling(self) -> None:
        assert _decide(quota=_quota(fresh=False), static_ceiling=None).ceiling == 4

    def test_an_unknown_quota_never_admits_wider_than_a_known_healthy_one(self) -> None:
        unknown = _decide(quota=_quota(fresh=False), static_ceiling=None)
        known_healthy = _decide(quota=_quota(), static_ceiling=None)
        assert unknown.ceiling <= known_healthy.ceiling

    def test_an_operator_ceiling_below_the_machine_one_still_wins(self) -> None:
        assert _decide(quota=_quota(fresh=False), static_ceiling=2).ceiling == 2

    def test_the_unknown_quota_ceiling_never_deadlocks_the_factory_to_zero(self) -> None:
        assert _decide(quota=_quota(fresh=False), machine=_machine(cores=1), static_ceiling=None).ceiling == 1

    def test_an_unknown_quota_scales_with_the_box_instead_of_pinning_to_one(self) -> None:
        # The #3644 defect this must not re-introduce was a hard clamp to 1 regardless
        # of the box; the machine-derived bound grows with the cores it is derived from.
        assert _decide(quota=_quota(fresh=False), machine=_machine(cores=32), static_ceiling=None).ceiling == 16


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


def _usage_row(path: str, *, utilization_7d: float, status_7d: str, valid_for: dt.timedelta) -> None:
    now = timezone.now()
    AnthropicTokenUsage.objects.create(
        pass_path=path,
        utilization_5h=0.1,
        utilization_7d=utilization_7d,
        status_5h="allowed",
        status_7d=status_7d,
        reset_7d=now + dt.timedelta(days=3),
        checked_at=now,
        valid_until=now + valid_for,
    )


def _healthy_row(path: str = "healthy", *, valid_for: dt.timedelta = dt.timedelta(minutes=5)) -> None:
    _usage_row(path, utilization_7d=0.39, status_7d="allowed", valid_for=valid_for)


def _exhausted_row(path: str = "exhausted", *, valid_for: dt.timedelta = dt.timedelta(days=2)) -> None:
    _usage_row(path, utilization_7d=1.0, status_7d="rejected", valid_for=valid_for)


class TestReadQuotaSignalFreshnessBias(TestCase):
    """The fleet aggregate must not be computed over a sample biased by exhaustion.

    ``AnthropicTokenUsage.valid_until`` is the ROUTING cache's "may I skip a
    re-probe?" rule, and it is deliberately asymmetric: a healthy verdict lapses
    after ``HEALTH_TTL`` (5 minutes) while an exhausted one is trusted until its
    blocking window resets (days). Filtering the fleet aggregate through that same
    rule is survivorship bias — outside the minutes right after a healthy probe the
    ONLY surviving rows are the exhausted ones, so ``all_accounts_exhausted`` reads
    True and the governor brakes the whole headless lane while a healthy account
    sits at 39% weekly. The aggregate must claim knowledge only when it knows every
    account; a partial sample is ``fresh=False``, which the decision fails OPEN on.
    """

    def test_a_lapsed_healthy_row_does_not_read_as_every_account_exhausted(self) -> None:
        _healthy_row(valid_for=dt.timedelta(minutes=-1))
        _exhausted_row()

        signal = admission_governor.read_quota_signal()

        assert signal.all_accounts_exhausted is False

    def test_a_lapsed_healthy_row_does_not_brake_the_headless_lane(self) -> None:
        _healthy_row(valid_for=dt.timedelta(minutes=-1))
        _exhausted_row()

        decision = decide_admission(
            quota=admission_governor.read_quota_signal(), machine=_machine(), static_ceiling=None
        )

        assert decision.admit is True

    def test_a_partial_sample_claims_no_knowledge(self) -> None:
        _healthy_row(valid_for=dt.timedelta(minutes=-1))
        _exhausted_row()

        assert admission_governor.read_quota_signal().fresh is False

    def test_every_account_fresh_and_exhausted_still_brakes(self) -> None:
        _exhausted_row("a")
        _exhausted_row("b")

        signal = admission_governor.read_quota_signal()

        assert signal.fresh is True
        assert signal.all_accounts_exhausted is True
        assert decide_admission(quota=signal, machine=_machine(), static_ceiling=None).admit is False

    def test_a_fully_fresh_mixed_fleet_reports_the_healthy_account(self) -> None:
        _healthy_row()
        _exhausted_row()

        signal = admission_governor.read_quota_signal()

        assert signal.fresh is True
        assert signal.all_accounts_exhausted is False
        assert signal.weekly_utilization == pytest.approx(0.39)

    def test_no_rows_at_all_still_reads_unknown(self) -> None:
        assert admission_governor.read_quota_signal().fresh is False


class TestReadQuotaSignalUsesTheFreshSubset(TestCase):
    """A usable account's own numbers must survive a lapsed PEER row.

    Whole-fleet freshness is the right bar for ``all_accounts_exhausted`` — that
    claim is about every account, so an unknown row defeats it. It is the wrong
    bar for the utilization the pace brake and the adaptive ceiling read: an
    exhausted row is fresh BY CONSTRUCTION (``valid_until`` is its blocking-window
    reset, days out) while a healthy one lapses in minutes, so on a small fleet
    whole-fleet freshness is a coincidence and both mechanisms sit dark, silently
    falling back to the static ceiling. A fresh, non-exhausted account knows its
    own headroom regardless of what a lapsed peer is doing.
    """

    def test_a_fresh_healthy_account_reports_its_own_headroom(self) -> None:
        _healthy_row()
        _exhausted_row("lapsed", valid_for=dt.timedelta(minutes=-1))

        signal = admission_governor.read_quota_signal()

        assert signal.fresh is True
        assert signal.all_accounts_exhausted is False
        assert signal.weekly_utilization == pytest.approx(0.39)

    def test_the_adaptive_ceiling_survives_a_lapsed_peer(self) -> None:
        _healthy_row()
        _exhausted_row("lapsed", valid_for=dt.timedelta(minutes=-1))

        decision = decide_admission(
            quota=admission_governor.read_quota_signal(), machine=_machine(cores=8), static_ceiling=8
        )

        assert decision.admit is True
        # floor(8 cores * WRITE_CONCURRENCY_PER_CORE) at a healthy pace; the subject here is
        # that a LAPSED peer does not blank the ceiling, not the constant's value.
        assert decision.ceiling == 4

    def test_the_pacing_brake_survives_a_lapsed_peer(self) -> None:
        _usage_row("paced", utilization_7d=0.97, status_7d="allowed", valid_for=dt.timedelta(minutes=5))
        _exhausted_row("lapsed", valid_for=dt.timedelta(minutes=-1))

        decision = decide_admission(
            quota=admission_governor.read_quota_signal(), machine=_machine(), static_ceiling=None
        )

        assert decision.admit is False
        assert "pace" in decision.reason


class TestMergeThroughputGatesNewIntake:
    """New coding work is paced by whether anything is actually LANDING (#4044).

    :class:`YieldSignal` asks "did tasks finish?" — and a task that finished by opening
    a PR nothing can merge answers yes. This asks what that cannot: is any of it
    landing? A factory whose tasks all complete while its PRs pile up is producing
    inventory, and each further claimed issue deepens the pile without making any of it
    likelier to land.
    """

    def test_every_open_pr_refused_by_the_sweep_is_a_stall(self) -> None:
        assert MergeSignal(fresh=True, open_prs=7, stuck_prs=7).stalled

    def test_unreadable_rows_never_brake(self) -> None:
        """Unknown never brakes — a probe that cannot answer must not halt the factory."""
        assert not MergeSignal(fresh=False, open_prs=9, stuck_prs=9).stalled

    def test_a_quiet_day_with_few_open_prs_is_not_a_stall(self) -> None:
        assert not MergeSignal(fresh=True, open_prs=1, stuck_prs=1).stalled

    def test_one_pr_still_moving_is_not_a_stall(self) -> None:
        """Self-releasing: the brake lifts on evidence, never on an operator re-enabling it."""
        assert not MergeSignal(fresh=True, open_prs=5, stuck_prs=4).stalled

    def test_the_generic_governor_is_deliberately_untouched(self) -> None:
        """Ship and review dispatch CLEAR the pile; braking them would deadlock it."""
        assert _decide().admit


class TestWriteConcurrencyRaise:
    """8 cores admit 4, and every brake that makes that safe still holds (#4069)."""

    def test_an_eight_core_box_admits_four(self) -> None:
        decision = _decide(machine=_machine(cores=8, load1=1.0))
        assert decision.admit
        assert decision.ceiling == 4

    def test_the_load_brake_still_denies_at_the_watermark(self) -> None:
        """The brake is what makes a higher ceiling safe to attempt — it must not have moved."""
        decision = _decide(machine=_machine(cores=8, load1=40.0))
        assert not decision.admit
        assert "load" in decision.reason

    def test_total_test_workers_stay_bounded_as_agents_rise(self) -> None:
        """The melt driver the old 0.25 was calibrated against, now bounded independently."""
        assert per_agent_test_workers(cores=8, active_agents=4) * 4 <= 8 * 2


class TestResumeAgentPopulation:
    """A session resume restores the whole previously-running fleet in one step (#4108).

    The restore is not a dispatch, so the dispatch-side ceiling never sees it. The
    population it re-creates is a HOST fact, so the bound is derived from the live
    machine reading — never from a per-lane concurrency setting, which bounds one lane
    and says nothing about how many agents the box is carrying in total.
    """

    def test_an_idle_box_carries_one_agent_per_core(self) -> None:
        assert resume_agent_ceiling(_machine(cores=8, load1=0.0)) == 8

    def test_the_ceiling_falls_as_live_load_eats_the_box(self) -> None:
        idle = resume_agent_ceiling(_machine(cores=8, load1=0.0))
        busy = resume_agent_ceiling(_machine(cores=8, load1=20.0))
        assert 1 <= busy < idle

    def test_the_recorded_meltdown_leaves_room_for_nothing_beyond_one(self) -> None:
        """Load 58 on 8 cores — the reading taken during the simultaneous restore."""
        assert resume_agent_ceiling(_machine(cores=8, load1=58.0)) == 1

    def test_the_ceiling_never_reaches_zero(self) -> None:
        assert resume_agent_ceiling(_machine(cores=1, load1=999.0)) == 1

    def test_a_fleet_within_the_ceiling_says_nothing(self) -> None:
        assert resume_shed_directive(restored=3, machine=_machine(cores=8, load1=0.0)) == ""

    def test_a_fleet_at_the_ceiling_says_nothing(self) -> None:
        assert resume_shed_directive(restored=8, machine=_machine(cores=8, load1=0.0)) == ""

    def test_an_over_ceiling_restore_names_the_count_and_the_ceiling(self) -> None:
        directive = resume_shed_directive(restored=12, machine=_machine(cores=8, load1=20.0))
        assert "12" in directive  # the restored count
        assert "4" in directive  # the ceiling the live reading leaves
        assert "shed" in directive.lower()

    def test_the_recorded_meltdown_restore_is_never_silent(self) -> None:
        assert resume_shed_directive(restored=12, machine=_machine(cores=8, load1=58.0)) != ""

    def test_the_bound_is_the_live_reading_not_a_per_lane_setting(self) -> None:
        """Same fleet, same cores — only the live load differs, and only one warns."""
        fleet = 6
        assert resume_shed_directive(restored=fleet, machine=_machine(cores=8, load1=0.0)) == ""
        assert resume_shed_directive(restored=fleet, machine=_machine(cores=8, load1=30.0)) != ""
