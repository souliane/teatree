"""A catalog that lost an overlay surface says so, rather than shrinking quietly.

A broken overlay entry point is skipped by design — one bad overlay must not fail
the core catalog. What it must not do is disappear: nothing downstream can detect
a smaller catalog. ``--require-executed`` still sees executions, a metered run
still meters real spend, the green proof still reads green, and every
overlay-specific scenario silently stopped being evaluated.
"""

from types import SimpleNamespace
from unittest.mock import patch

from teatree.cli.eval.all import catalog_discovery_lane
from teatree.eval.discovery import ScenarioCatalog, discover_catalog


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


class TestCatalogDiscoveryLane:
    def test_the_lane_fails_and_names_the_overlay_that_contributed_nothing(self) -> None:
        catalog = ScenarioCatalog(specs=[], degraded={"t3-acme": "get_eval_scenarios_dir() failed: OSError: boom"})
        lane = catalog_discovery_lane(catalog)
        assert lane.passed is False
        assert "t3-acme" in lane.detail

    def test_a_complete_catalog_passes(self) -> None:
        catalog = ScenarioCatalog(specs=[], degraded={})
        lane = catalog_discovery_lane(catalog)
        assert lane.passed is True
        assert lane.skipped is False
