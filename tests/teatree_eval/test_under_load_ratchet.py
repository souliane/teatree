"""The shrink-only under_load ratchet (`teatree.eval.under_load_ratchet`).

The ratchet lets the metered under_load lane pass with a documented known-red set
while keeping teeth: a NEW under_load failure beyond the baseline is RED, and a
baselined scenario that starts passing must be REMOVED (the set only shrinks).
These tests prove the gate is anti-vacuous — it CANNOT be satisfied by an arbitrary
run — and that the checked-in baseline names only real under_load scenarios.
"""

from pathlib import Path

import pytest

from teatree.eval.discovery import discover_specs
from teatree.eval.models import UNDER_LOAD_LANE
from teatree.eval.under_load_ratchet import (
    UNDER_LOAD_KNOWN_RED_PATH,
    UnderLoadKnownRed,
    UnderLoadRatchetError,
    UnderLoadViolationKind,
    check_under_load_ratchet,
    load_under_load_known_red,
)


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "under_load_known_red.yaml"
    path.write_text(body, encoding="utf-8")
    return path


_TWO = "known_red:\n  - alpha\n  - beta\n"
_EMPTY = "known_red: []\n"
_DUPE = "known_red:\n  - alpha\n  - alpha\n"
_NOT_A_LIST = "known_red: 7\n"
_NOT_A_STRING = "known_red:\n  - 7\n"
_NOT_A_MAPPING = "- alpha\n- beta\n"


class TestLoad:
    def test_parses_the_known_red_list_into_a_frozenset(self, tmp_path: Path) -> None:
        config = load_under_load_known_red(_write(tmp_path, _TWO))
        assert config.known_red == frozenset({"alpha", "beta"})

    def test_empty_list_is_a_valid_zero_baseline(self, tmp_path: Path) -> None:
        # A drained baseline (the goal) is valid: nothing known-red, so any
        # under_load failure is a regression.
        assert load_under_load_known_red(_write(tmp_path, _EMPTY)).known_red == frozenset()

    def test_missing_file_raises_not_returns_empty(self, tmp_path: Path) -> None:
        # An absent baseline must be a hard error, never a vacuously-green empty set.
        with pytest.raises(UnderLoadRatchetError, match="missing"):
            load_under_load_known_red(tmp_path / "does_not_exist.yaml")

    def test_duplicate_entry_raises(self, tmp_path: Path) -> None:
        with pytest.raises(UnderLoadRatchetError, match="duplicate"):
            load_under_load_known_red(_write(tmp_path, _DUPE))

    def test_non_list_known_red_raises(self, tmp_path: Path) -> None:
        with pytest.raises(UnderLoadRatchetError, match="must be a list"):
            load_under_load_known_red(_write(tmp_path, _NOT_A_LIST))

    def test_non_string_entry_raises(self, tmp_path: Path) -> None:
        with pytest.raises(UnderLoadRatchetError, match="scenario name string"):
            load_under_load_known_red(_write(tmp_path, _NOT_A_STRING))

    def test_non_mapping_top_level_raises(self, tmp_path: Path) -> None:
        with pytest.raises(UnderLoadRatchetError, match="top-level mapping"):
            load_under_load_known_red(_write(tmp_path, _NOT_A_MAPPING))


_BASELINE = UnderLoadKnownRed(known_red=frozenset({"alpha", "beta"}))


class TestRatchetHasTeeth:
    def test_failing_set_equals_baseline_passes(self) -> None:
        # The documented known-red set fails, nothing else — the lane passes.
        result = check_under_load_ratchet(["alpha", "beta"], [], _BASELINE)
        assert not result.failed
        assert result.violations == ()

    def test_new_failure_beyond_baseline_is_a_regression(self) -> None:
        # ANTI-VACUITY (regression direction): a fresh under_load red OUTSIDE the
        # baseline makes the gate RED — the ratchet is not vacuously satisfied.
        result = check_under_load_ratchet(["alpha", "beta", "gamma"], [], _BASELINE)
        assert result.failed
        kinds = {(v.scenario_name, v.kind) for v in result.violations}
        assert ("gamma", UnderLoadViolationKind.REGRESSION) in kinds

    def test_baselined_scenario_that_now_passes_must_be_removed(self) -> None:
        # ANTI-VACUITY (shrink-only direction): a baselined scenario left in the
        # file while it now PASSES makes the gate RED — the set may only shrink.
        result = check_under_load_ratchet(["alpha"], ["beta"], _BASELINE)
        assert result.failed
        kinds = {(v.scenario_name, v.kind) for v in result.violations}
        assert ("beta", UnderLoadViolationKind.STALE) in kinds

    def test_shrinking_the_baseline_after_a_fix_passes(self) -> None:
        # The intended workflow: beta is fixed AND removed from the baseline → green.
        shrunk = UnderLoadKnownRed(known_red=frozenset({"alpha"}))
        result = check_under_load_ratchet(["alpha"], ["beta"], shrunk)
        assert not result.failed

    def test_both_violation_directions_can_fire_in_one_run(self) -> None:
        # A new red (gamma) AND a now-passing baselined scenario (beta) both surface.
        result = check_under_load_ratchet(["alpha", "gamma"], ["beta"], _BASELINE)
        kinds = {(v.scenario_name, v.kind) for v in result.violations}
        assert ("gamma", UnderLoadViolationKind.REGRESSION) in kinds
        assert ("beta", UnderLoadViolationKind.STALE) in kinds

    def test_skipped_scenario_neither_passes_nor_fails(self) -> None:
        # A key-less all-skipped run reports NO under_load scenario as passing OR
        # failing, so the gate finds no regression and no stale baseline — it does
        # not spuriously RED just because a baselined scenario did not execute.
        result = check_under_load_ratchet([], [], _BASELINE)
        assert not result.failed

    def test_violation_render_names_the_actionable_fix(self) -> None:
        regression = check_under_load_ratchet(["gamma"], [], UnderLoadKnownRed(frozenset())).violations[0]
        assert "do NOT widen the baseline" in regression.render()
        stale = check_under_load_ratchet([], ["alpha"], UnderLoadKnownRed(frozenset({"alpha"}))).violations[0]
        assert "REMOVE this entry" in stale.render()


class TestCheckedInBaselineIsReal:
    def test_loads_without_error(self) -> None:
        # The committed file parses — a malformed baseline is a hard RED here, not
        # at the weekly metered run.
        baseline = load_under_load_known_red()
        assert baseline.known_red, "the checked-in baseline must be non-empty until the behavioural fix drains it"

    def test_every_baselined_scenario_is_a_real_under_load_scenario(self) -> None:
        # A typo'd or stale name in the baseline (a ghost scenario the metered run
        # never reports) would silently never trip the regression direction. Pin
        # every entry to an actually-discovered under_load scenario.
        under_load_names = {spec.name for spec in discover_specs() if spec.lane == UNDER_LOAD_LANE}
        baseline = load_under_load_known_red()
        ghosts = sorted(baseline.known_red - under_load_names)
        assert not ghosts, (
            f"under_load_known_red.yaml names scenario(s) {ghosts!r} that are not discovered "
            f"under_load scenarios. A ghost entry can never trip the ratchet — remove it or fix "
            f"the name. Known under_load scenarios: {sorted(under_load_names)}"
        )

    def test_baseline_path_points_at_the_committed_file(self) -> None:
        assert UNDER_LOAD_KNOWN_RED_PATH.name == "under_load_known_red.yaml"
        assert UNDER_LOAD_KNOWN_RED_PATH.is_file()
