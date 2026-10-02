"""A catalog that lost an overlay surface says so, rather than shrinking quietly.

A broken overlay entry point is skipped by design — one bad overlay must not fail
the core catalog. What it must not do is disappear: nothing downstream can detect
a smaller catalog. ``--require-executed`` still sees executions, a metered run
still meters real spend, the green proof still reads green, and every
overlay-specific scenario silently stopped being evaluated.

A hook that RAISED is only one of the two ways an overlay contributes nothing.
The other is a hook that SUCCEEDED while naming a directory that is not there —
same short denominator, same green, and until #4373 the same silence.
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from teatree.cli.eval.all import catalog_discovery_lane
from teatree.eval.discovery import CORE_CATALOG_FLOOR, ScenarioCatalog, discover_catalog


def _explode() -> dict[str, object]:
    msg = "overlay entry point raised on import"
    raise RuntimeError(msg)


class TestCatalogRecordsItsDegradation:
    def test_an_unreadable_overlay_registry_is_recorded_on_the_catalog(self) -> None:
        with patch("teatree.core.overlay_loader.get_all_overlays", side_effect=_explode):
            catalog = discover_catalog()
        assert not catalog.is_complete
        assert "overlay registry unreadable" in catalog.degraded["*"]
        # The core catalog is still returned — the skip is real, only the silence is not.
        assert catalog.specs

    def test_a_raising_overlay_hook_is_recorded_under_its_own_name(self) -> None:
        def _bad() -> str:
            msg = "no scenarios dir"
            raise OSError(msg)

        overlay = SimpleNamespace(get_eval_scenarios_dir=_bad)
        with patch("teatree.core.overlay_loader.get_all_overlays", return_value={"t3-bad": overlay}):
            catalog = discover_catalog()
        assert "t3-bad" in catalog.degraded

    def test_an_overlay_with_no_scenarios_dir_is_not_a_degradation(self) -> None:
        # The anti-vacuity control: declining to contribute is not failing to.
        overlay = SimpleNamespace(get_eval_scenarios_dir=lambda: None)
        with patch("teatree.core.overlay_loader.get_all_overlays", return_value={"t3-quiet": overlay}):
            catalog = discover_catalog()
        assert catalog.is_complete
        assert catalog.degraded == {}

    def test_an_overlay_naming_a_missing_dir_is_recorded_under_its_own_name(self, tmp_path: Path) -> None:
        moved_away = tmp_path / "eval" / "scenarios"
        overlay = SimpleNamespace(get_eval_scenarios_dir=lambda: moved_away)
        with patch("teatree.core.overlay_loader.get_all_overlays", return_value={"t3-moved": overlay}):
            catalog = discover_catalog()
        assert not catalog.is_complete
        assert str(moved_away) in catalog.degraded["t3-moved"]

    def test_the_two_zero_contribution_routes_are_distinguishable(self, tmp_path: Path) -> None:
        # Only one of the two is a defect, so the catalog must not report them alike.
        overlays = {
            "t3-quiet": SimpleNamespace(get_eval_scenarios_dir=lambda: None),
            "t3-moved": SimpleNamespace(get_eval_scenarios_dir=lambda: tmp_path / "gone"),
        }
        with patch("teatree.core.overlay_loader.get_all_overlays", return_value=overlays):
            catalog = discover_catalog()
        assert sorted(catalog.degraded) == ["t3-moved"]


class TestCatalogDiscoveryLane:
    def test_the_lane_fails_and_names_the_overlay_that_contributed_nothing(self) -> None:
        catalog = ScenarioCatalog(
            specs=[],
            degraded={"t3-acme": "get_eval_scenarios_dir() failed: OSError: boom"},
            core_count=CORE_CATALOG_FLOOR,
        )
        lane = catalog_discovery_lane(catalog)
        assert lane.passed is False
        assert "t3-acme" in lane.detail

    def test_a_complete_catalog_passes(self) -> None:
        catalog = ScenarioCatalog(specs=[], degraded={}, core_count=CORE_CATALOG_FLOOR)
        lane = catalog_discovery_lane(catalog)
        assert lane.passed is True
        assert lane.skipped is False

    def test_the_lane_reports_the_core_count_against_the_floor(self) -> None:
        catalog = ScenarioCatalog(specs=[], degraded={}, core_count=CORE_CATALOG_FLOOR)
        assert f"floor {CORE_CATALOG_FLOOR}" in catalog_discovery_lane(catalog).detail

    def test_a_core_catalog_below_the_floor_fails_the_lane(self) -> None:
        # The denominator shrinking by two was invisible in #4373; a floor is what
        # makes a shrink cost an edit rather than nothing.
        catalog = ScenarioCatalog(specs=[], degraded={}, core_count=CORE_CATALOG_FLOOR - 2)
        lane = catalog_discovery_lane(catalog)
        assert lane.passed is False
        assert str(CORE_CATALOG_FLOOR - 2) in lane.detail
