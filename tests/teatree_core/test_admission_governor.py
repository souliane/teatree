"""The adaptive admission governor (#3644).

Token budget is the PRIMARY signal and machine pressure the secondary one, so the
matrix below always states both: an idle box with no weekly quota admits NOTHING,
and a healthy weekly window still yields to a melting box. Every refusal carries a
reason — a governor that denies silently recreates the class of bug that hid a dead
merge loop for weeks.
"""

import datetime as dt

import pytest
from django.test import TestCase
from django.utils import timezone

from teatree.agents import _headless_env
from teatree.agents._headless_env import XDIST_WORKERS_VAR, with_test_worker_cap
from teatree.core import admission_governor
from teatree.core.admission_governor import (
    MachineSignal,
    MergeSignal,
    QuotaSignal,
    YieldSignal,
    decide_admission,
    merge_pressure,
    per_agent_test_workers,
    read_machine_signal,
    weekly_pace,
)
from teatree.core.models.anthropic_token_usage import AnthropicTokenUsage

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

    def test_pressure_eases_the_ceiling_back_rather_than_flipping(self) -> None:
        draining = merge_pressure(MergeSignal(fresh=True, open_prs=8, stuck_prs=6))
        clear = merge_pressure(MergeSignal(fresh=True, open_prs=8, stuck_prs=0))
        assert clear == pytest.approx(1.0)
        assert 0.0 < draining < clear

    def test_pressure_is_unclamped_when_unknown_or_below_threshold(self) -> None:
        assert merge_pressure(None) == pytest.approx(1.0)
        assert merge_pressure(MergeSignal(fresh=False, open_prs=9, stuck_prs=9)) == pytest.approx(1.0)
        assert merge_pressure(MergeSignal(fresh=True, open_prs=2, stuck_prs=2)) == pytest.approx(1.0)

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
