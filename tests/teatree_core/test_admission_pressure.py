"""The one admission-pressure scalar (#4508).

Six brakes evaluated as six independent ifs cannot be ordered, so nothing could
say "degrade a little" — only admit or refuse. The scalar's whole correctness
argument is that ``1.0`` IS each dimension's own existing watermark, which is
what makes the HALT verdict a re-expression of today's brakes rather than a new
opinion; the equivalence grid below is that proof.
"""

import math

import pytest

from teatree.core.admission_pressure import (
    BRAKE_LOAD_PER_CORE,
    DEGRADE_AT,
    HALT_AT,
    PACE_DENY,
    RAM_BRAKE_FLOOR_GB,
    RAM_RESUME_FLOOR_GB,
    RESUME_LOAD_PER_CORE,
    SHED_AT_DEFAULT,
    SHORT_WINDOW_BRAKE,
    WEEKLY_WINDOW_BRAKE,
    AdmissionPressure,
    MachineSignal,
    PressureBand,
    QuotaSignal,
    admission_pressure,
    box_load_headroom,
    ram_headroom,
    resolve_shed_at,
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


def _at_pace_deny() -> QuotaSignal:
    """A quota whose pace is EXACTLY ``PACE_DENY`` — the boundary the fold moves.

    Both terms are dyadic (``1 - 0.9375`` is exact, as is ``0.625``) so the quotient
    rounds to the very double ``PACE_DENY`` names. The obvious spelling
    ``weekly_utilization=1.0 - PACE_DENY`` does NOT reach the boundary: it lands a
    half-ulp under it and the old brake fires, which is not the case under test.
    """
    return _quota(weekly_utilization=0.9375, seconds_to_weekly_reset=_WEEK * 0.625)


def _named(pressure: AdmissionPressure, name: str) -> float:
    return next(component.value for component in pressure.components if component.name == name)


# The pre-#4508 brake predicates, frozen here as the oracle the scalar must reproduce.
# Copied from the shipped `_quota_brake` / `_machine_brake` at c9ea95de, not paraphrased:
# a paraphrase would prove the paraphrase.
def _old_quota_brake(quota: QuotaSignal) -> str:
    if not quota.fresh:
        return ""
    if quota.all_accounts_exhausted:
        return "every account is quota-exhausted — retrying into a rate limit is pure burn"
    if quota.weekly_utilization >= WEEKLY_WINDOW_BRAKE:
        return f"weekly window spent ({quota.weekly_utilization:.0%}) — no budget left to admit against"
    if quota.short_utilization >= SHORT_WINDOW_BRAKE:
        return f"5h window spent ({quota.short_utilization:.0%}) — a hard rate limit is imminent"
    if weekly_pace(quota) < PACE_DENY:
        return f"weekly burn outruns the reset (pace {weekly_pace(quota):.2f}) — pacing to the window"
    return ""


def _old_machine_brake(machine: MachineSignal, *, braked: bool) -> str:
    cores = max(1, machine.cores)
    watermark = (RESUME_LOAD_PER_CORE if braked else BRAKE_LOAD_PER_CORE) * cores
    if machine.load1 >= watermark:
        return f"load {machine.load1:.0f} at/over the {watermark:.0f} watermark on {cores} core(s)"
    floor = RAM_RESUME_FLOOR_GB if braked else RAM_BRAKE_FLOOR_GB
    if machine.ram_available_gb is not None and machine.ram_available_gb <= floor:
        return f"{machine.ram_available_gb:.1f} GB available at/under the {floor:.0f} GB watermark"
    return ""


def _old_reason(quota: QuotaSignal, machine: MachineSignal, *, braked: bool, applies: bool = True) -> str:
    """The pre-#4508 refusal: quota brakes first, then machine — first match wins."""
    return _old_quota_brake(quota) or (_old_machine_brake(machine, braked=braked) if applies else "")


def _old_quota_denies(quota: QuotaSignal) -> bool:
    return bool(_old_quota_brake(quota))


class TestComponentNormalization:
    """Each dimension reads 1.0 at exactly the watermark its old brake denied on."""

    def test_weekly_quota_reaches_one_at_the_brake(self) -> None:
        at = admission_pressure(quota=_quota(weekly_utilization=WEEKLY_WINDOW_BRAKE), machine=_machine())
        assert _named(at, "weekly-quota") == pytest.approx(1.0)
        under = admission_pressure(quota=_quota(weekly_utilization=WEEKLY_WINDOW_BRAKE - 0.01), machine=_machine())
        assert _named(under, "weekly-quota") < 1.0

    def test_short_quota_reaches_one_at_the_brake(self) -> None:
        at = admission_pressure(quota=_quota(short_utilization=SHORT_WINDOW_BRAKE), machine=_machine())
        assert _named(at, "5h-quota") == pytest.approx(1.0)

    def test_exhaustion_is_a_flat_one(self) -> None:
        at = admission_pressure(quota=_quota(all_accounts_exhausted=True), machine=_machine())
        assert _named(at, "accounts-exhausted") == pytest.approx(1.0)
        assert at.band is PressureBand.HALT

    def test_load_reaches_one_at_the_watermark(self) -> None:
        watermark = BRAKE_LOAD_PER_CORE * 8
        at = admission_pressure(quota=_quota(), machine=_machine(load1=watermark))
        assert _named(at, "load") == pytest.approx(1.0)

    def test_load_watermark_rides_the_hysteresis(self) -> None:
        """A braked governor is held to the LOWER watermark, so the same load reads higher."""
        braked = admission_pressure(quota=_quota(), machine=_machine(load1=RESUME_LOAD_PER_CORE * 8), braked=True)
        assert _named(braked, "load") == pytest.approx(1.0)
        free = admission_pressure(quota=_quota(), machine=_machine(load1=RESUME_LOAD_PER_CORE * 8))
        assert _named(free, "load") < 1.0

    def test_memory_reaches_one_at_the_floor(self) -> None:
        at = admission_pressure(quota=_quota(), machine=_machine(ram_available_gb=RAM_BRAKE_FLOOR_GB))
        assert _named(at, "memory") == pytest.approx(1.0)
        assert admission_pressure(quota=_quota(), machine=_machine(ram_available_gb=2.0)).value > 1.0

    def test_pace_reaches_one_at_the_deny_floor(self) -> None:
        assert weekly_pace(_at_pace_deny()) == PACE_DENY
        assert _named(admission_pressure(quota=_at_pace_deny(), machine=_machine()), "weekly-pace") == pytest.approx(
            1.0
        )

    def test_a_component_never_reads_below_zero(self) -> None:
        """An over-provisioned window is inert, never a negative that drags the max down."""
        roomy = admission_pressure(
            quota=_quota(weekly_utilization=0.0, seconds_to_weekly_reset=_WEEK), machine=_machine()
        )
        assert _named(roomy, "weekly-pace") == pytest.approx(0.0)
        assert _named(roomy, "memory") == pytest.approx(0.0)


def _headroom_of(component: float) -> float:
    """``1 - component``, clamped the way a ceiling multiplier must be."""
    return min(1.0, max(0.0, 1.0 - component))


class TestAlgebraicIdentity:
    """The scalar's headroom IS the existing readers' headroom — not a second opinion.

    This is what makes the intake change a GENERALISATION rather than a new clamp: on a
    CPU-bound box ``1 - pressure`` is byte-identical to the load headroom intake already
    consumed, so the number moves only when a quota dimension dominates.
    """

    @pytest.mark.parametrize("load1", [0.0, 3.0, 12.0, 25.0, 39.0, 40.0, 60.0])
    def test_load_headroom_matches_box_load_headroom(self, load1: float) -> None:
        pressure = admission_pressure(quota=_quota(), machine=_machine(load1=load1, ram_available_gb=None))
        assert _headroom_of(_named(pressure, "load")) == pytest.approx(box_load_headroom(load1=load1, cores=8))

    @pytest.mark.parametrize("available", [2.0, 4.0, 4.5, 5.0, 6.0, 9.0, 20.0])
    def test_memory_headroom_matches_the_resume_ramp(self, available: float) -> None:
        pressure = admission_pressure(quota=_quota(), machine=_machine(ram_available_gb=available))
        assert _headroom_of(_named(pressure, "memory")) == pytest.approx(ram_headroom(available))


class TestHaltEquivalence:
    """HALT reproduces the six pre-#4508 brakes exactly — the refactor proof."""

    @pytest.mark.parametrize("weekly", [0.0, 0.5, 0.9, 0.98, 0.99, 1.0])
    @pytest.mark.parametrize("short", [0.0, 0.5, 0.94, 0.95, 1.0])
    @pytest.mark.parametrize("exhausted", [False, True])
    @pytest.mark.parametrize("braked", [False, True])
    def test_quota_grid(
        self,
        weekly: float,
        short: float,
        exhausted: bool,  # noqa: FBT001 — parametrized matrix dimension, not a flag arg.
        braked: bool,  # noqa: FBT001 — parametrized matrix dimension, not a flag arg.
    ) -> None:
        quota = _quota(weekly_utilization=weekly, short_utilization=short, all_accounts_exhausted=exhausted)
        pressure = admission_pressure(quota=quota, machine=_machine(), braked=braked)
        old = _old_reason(quota, _machine(), braked=braked)
        assert (pressure.band is PressureBand.HALT) is bool(old)
        if old:
            # The CAUSE too, not just the verdict: a refusal that renames itself is a
            # different refusal to whoever reads it.
            assert pressure.reason == old

    @pytest.mark.parametrize("load1", [0.0, 10.0, 23.9, 24.0, 39.9, 40.0, 55.0])
    @pytest.mark.parametrize("ram", [None, 2.0, 4.0, 5.0, 6.0, 20.0])
    @pytest.mark.parametrize("braked", [False, True])
    def test_machine_grid(
        self,
        load1: float,
        ram: float | None,
        braked: bool,  # noqa: FBT001 — parametrized matrix dimension, not a flag arg.
    ) -> None:
        machine = _machine(load1=load1, ram_available_gb=ram)
        pressure = admission_pressure(quota=_quota(), machine=machine, braked=braked)
        old = _old_reason(_quota(), machine, braked=braked)
        assert (pressure.band is PressureBand.HALT) is bool(old)
        if old:
            assert pressure.reason == old

    def test_an_exhausted_fleet_is_named_exhausted_not_its_collapsed_pace(self) -> None:
        """Precedence, not magnitude: exhaustion drives pace to zero, so max() would rename it."""
        spent = _quota(
            all_accounts_exhausted=True, weekly_utilization=1.0, short_utilization=1.0, seconds_to_weekly_reset=100.0
        )
        pressure = admission_pressure(quota=spent, machine=_machine())
        assert _named(pressure, "weekly-pace") > _named(pressure, "accounts-exhausted")
        assert pressure.dominant is not None
        assert pressure.dominant.name == "accounts-exhausted"
        assert pressure.reason == _old_reason(spent, _machine(), braked=False)

    def test_pace_boundary_is_the_one_deliberate_change(self) -> None:
        """Pace alone denied at ``< PACE_DENY``; the unified ``>= HALT_AT`` denies AT it.

        The other five dimensions already denied at ``>=`` their watermark. Folding
        them into one predicate makes pace agree, moving in the conservative
        direction on a measure-zero boundary.
        """
        assert _old_quota_denies(_at_pace_deny()) is False
        assert admission_pressure(quota=_at_pace_deny(), machine=_machine()).band is PressureBand.HALT


class TestUnknownNeverBrakes:
    def test_a_stale_quota_contributes_no_component(self) -> None:
        pressure = admission_pressure(quota=_quota(fresh=False, weekly_utilization=1.0), machine=_machine())
        assert [component.name for component in pressure.components] == ["load", "memory"]
        assert pressure.band is PressureBand.FULL

    def test_an_unread_memory_reading_contributes_no_component(self) -> None:
        pressure = admission_pressure(quota=_quota(), machine=_machine(ram_available_gb=None))
        assert "memory" not in [component.name for component in pressure.components]

    def test_the_cheap_lane_exemption_drops_both_machine_components(self) -> None:
        """The exemption is from MACHINE pressure only — a token brake still halts it."""
        molten = _machine(load1=99.0, ram_available_gb=0.5)
        assert admission_pressure(quota=_quota(), machine=molten, machine_applies=False).band is PressureBand.FULL
        spent = _quota(weekly_utilization=1.0)
        assert admission_pressure(quota=spent, machine=molten, machine_applies=False).band is PressureBand.HALT

    def test_nothing_readable_is_zero_pressure_with_no_dominant(self) -> None:
        blind = admission_pressure(
            quota=_quota(fresh=False), machine=_machine(ram_available_gb=None), machine_applies=False
        )
        assert blind.value == pytest.approx(0.0)
        assert blind.dominant is None
        assert blind.band is PressureBand.FULL
        assert blind.reason == ""


class TestBands:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (0.0, PressureBand.FULL),
            (0.69, PressureBand.FULL),
            (DEGRADE_AT, PressureBand.DEGRADED),
            (0.89, PressureBand.DEGRADED),
            (SHED_AT_DEFAULT, PressureBand.SHED),
            (0.99, PressureBand.SHED),
            (HALT_AT, PressureBand.HALT),
            (3.0, PressureBand.HALT),
        ],
    )
    def test_thresholds(self, value: float, expected: PressureBand) -> None:
        assert PressureBand.for_value(value, shed_at=SHED_AT_DEFAULT) is expected

    def test_the_dominant_component_names_the_reason(self) -> None:
        # A near-spent window whose reset is imminent: pace is fine (the little that is
        # left lasts the little that remains), so the SPEND itself is the worst dimension.
        spent = _quota(weekly_utilization=0.98, seconds_to_weekly_reset=_WEEK * 0.02)
        pressure = admission_pressure(quota=spent, machine=_machine(load1=39.0))
        assert pressure.dominant is not None
        assert pressure.dominant.name == "weekly-quota"
        assert "weekly window" in pressure.reason
        assert pressure.band is PressureBand.SHED

    def test_a_long_runway_makes_the_burn_rate_the_worst_dimension_not_the_spend(self) -> None:
        """Same 98% spend, half a week to go: what is wrong is the BURN RATE, and it says so."""
        pressure = admission_pressure(quota=_quota(weekly_utilization=0.98), machine=_machine())
        assert pressure.dominant is not None
        assert pressure.dominant.name == "weekly-pace"

    def test_the_value_is_the_worst_dimension(self) -> None:
        pressure = admission_pressure(quota=_quota(weekly_utilization=0.5), machine=_machine(load1=36.0))
        assert pressure.value == pytest.approx(max(component.value for component in pressure.components))


class TestShedAtResolution:
    """A misconfigured threshold clamps — it can never wedge the expensive lane shut."""

    @pytest.mark.parametrize(
        ("configured", "expected"),
        [
            (0.9, 0.9),
            (0.75, 0.75),
            (1.0, 1.0),
            (0.0, DEGRADE_AT),
            (-3.0, DEGRADE_AT),
            (5.0, HALT_AT),
            (math.nan, SHED_AT_DEFAULT),
        ],
    )
    def test_clamped_into_the_documented_range(self, configured: float, expected: float) -> None:
        assert resolve_shed_at(configured) == pytest.approx(expected)

    def test_one_collapses_shed_into_halt(self) -> None:
        """The rollback lever: at 1.0 no value can land in SHED."""
        for value in (0.9, 0.95, 0.999):
            assert PressureBand.for_value(value, shed_at=1.0) is PressureBand.DEGRADED
