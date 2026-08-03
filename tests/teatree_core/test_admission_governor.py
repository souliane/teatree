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
    QuotaSignal,
    YieldSignal,
    decide_admission,
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
        assert _headless_env.with_test_worker_cap(None, active_agents=4) is None
        assert _headless_env.with_test_worker_cap({"A": "b"}, active_agents=4) == {"A": "b"}


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

    An unknown quota used to leave ``static_ceiling`` verbatim, so the headless lane —
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
