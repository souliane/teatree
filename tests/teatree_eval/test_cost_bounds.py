"""The declarative per-scenario cost ceiling gate (`teatree.eval.cost_bounds`)."""

from pathlib import Path

import pytest

from teatree.eval.cost_bounds import (
    CostBoundsConfig,
    CostBoundsError,
    CostBoundViolationKind,
    ScenarioCostBound,
    check_cost_bounds,
    load_cost_bounds,
)


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "cost_bounds.yaml"
    path.write_text(body, encoding="utf-8")
    return path


_TWO_BOUNDS = (
    "default_margin: 0.25\nscenarios:\n  alpha:\n    bound_usd: 0.10\n  beta:\n    bound_usd: 0.20\n    margin: 0.50\n"
)
_ALPHA_BOUND = "default_margin: 0.20\nscenarios:\n  alpha:\n    bound_usd: 0.10\n"
_EMPTY = "default_margin: 0.25\nscenarios: {}\n"
_MARGIN_ONLY = "scenarios:\n  alpha:\n    margin: 0.1\n"
_STRING_BOUND = "scenarios:\n  alpha:\n    bound_usd: lots\n"
_NEGATIVE_BOUND = "scenarios:\n  alpha:\n    bound_usd: -0.10\n"


class TestLoadCostBounds:
    def test_parses_default_margin_and_per_scenario_bounds(self, tmp_path: Path) -> None:
        path = _write(tmp_path, _TWO_BOUNDS)
        config = load_cost_bounds(path)
        assert config.default_margin == pytest.approx(0.25)
        assert config.bounds["alpha"] == ScenarioCostBound("alpha", 0.10, 0.25)
        assert config.bounds["beta"] == ScenarioCostBound("beta", 0.20, 0.50)

    def test_ceiling_applies_the_margin(self, tmp_path: Path) -> None:
        path = _write(tmp_path, _ALPHA_BOUND)
        config = load_cost_bounds(path)
        assert config.bounds["alpha"].ceiling_usd == pytest.approx(0.12)

    def test_empty_scenarios_loads_clean(self, tmp_path: Path) -> None:
        path = _write(tmp_path, _EMPTY)
        config = load_cost_bounds(path)
        assert config.bounds == {}

    def test_missing_file_is_fail_loud(self, tmp_path: Path) -> None:
        with pytest.raises(CostBoundsError, match="missing"):
            load_cost_bounds(tmp_path / "absent.yaml")

    def test_bound_missing_required_field_raises(self, tmp_path: Path) -> None:
        path = _write(tmp_path, _MARGIN_ONLY)
        with pytest.raises(CostBoundsError, match="bound_usd"):
            load_cost_bounds(path)

    def test_non_numeric_bound_raises(self, tmp_path: Path) -> None:
        path = _write(tmp_path, _STRING_BOUND)
        with pytest.raises(CostBoundsError, match="must be a number"):
            load_cost_bounds(path)

    def test_negative_bound_raises(self, tmp_path: Path) -> None:
        path = _write(tmp_path, _NEGATIVE_BOUND)
        with pytest.raises(CostBoundsError, match="non-negative"):
            load_cost_bounds(path)


def _config() -> CostBoundsConfig:
    return CostBoundsConfig(
        default_margin=0.25,
        bounds={
            "alpha": ScenarioCostBound("alpha", 0.10, 0.20),
            "beta": ScenarioCostBound("beta", 0.50, 0.25),
        },
    )


class TestCheckCostBounds:
    def test_over_ceiling_is_a_violation(self) -> None:
        result = check_cost_bounds({"alpha": 0.30, "beta": 0.40}, _config())
        assert result.failed
        names = {v.scenario_name: v.kind for v in result.violations}
        assert names["alpha"] is CostBoundViolationKind.OVER
        assert "beta" not in names

    def test_at_or_under_ceiling_passes(self) -> None:
        # alpha ceiling = 0.10 * 1.20 = 0.12; exactly 0.12 is OK, 0.40 is well under beta's 0.625.
        result = check_cost_bounds({"alpha": 0.12, "beta": 0.40}, _config())
        assert not result.failed
        assert result.violations == []
        assert result.checked == 2

    def test_missing_recorded_cost_is_fail_loud(self) -> None:
        # beta is configured but the run carries no cost for it.
        result = check_cost_bounds({"alpha": 0.05}, _config())
        assert result.failed
        kinds = {v.scenario_name: v.kind for v in result.violations}
        assert kinds == {"beta": CostBoundViolationKind.MISSING}

    def test_zero_recorded_cost_is_missing_not_pass(self) -> None:
        # A configured scenario that metered $0 is fail-loud, never skip-as-pass.
        result = check_cost_bounds({"alpha": 0.0, "beta": 0.40}, _config())
        assert result.failed
        assert result.violations[0].scenario_name == "alpha"
        assert result.violations[0].kind is CostBoundViolationKind.MISSING

    def test_unbounded_scenario_in_run_is_ignored(self) -> None:
        result = check_cost_bounds({"alpha": 0.05, "beta": 0.40, "gamma": 99.0}, _config())
        assert not result.failed

    def test_violation_render_distinguishes_over_and_missing(self) -> None:
        result = check_cost_bounds({"alpha": 0.30}, _config())
        rendered = {v.scenario_name: v.render() for v in result.violations}
        assert "OVER BOUND alpha" in rendered["alpha"]
        assert "MISSING beta" in rendered["beta"]
