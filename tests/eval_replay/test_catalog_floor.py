"""The scenario catalog cannot silently shrink — a count-floor + an existence guard.

A mis-pointed move (off-by-one dir, a typo'd ``SCENARIOS_DIR``) makes
``SCENARIOS_DIR.glob("*.yaml")`` return ``[]`` without raising, so a metered
``run --backend api`` would execute only whatever the overlays contribute, meter
``>$0``, and exit GREEN — ``assert_executed_when_required`` only fires on
``executed == 0``. Two deterministic guards close that hole:

*   a floor: :data:`~teatree.eval.discovery.CORE_CATALOG_FLOOR`, a shrink-only
    ratchet close to the shipped count, so losing even a couple of scenarios costs
    an edit to a named constant rather than nothing (#4373 lost two invisibly);
*   an existence check: a missing ``SCENARIOS_DIR`` raises rather than yielding
    an empty catalog.

The floor is on the CORE catalog alone. An installed overlay only ever ADDS to the
total, so flooring the total would red an install whose overlay legitimately
contributes none.
"""

import pytest

from teatree.eval import discovery
from teatree.eval.discovery import CORE_CATALOG_FLOOR, ScenarioCatalogError, discover_core_specs, discover_specs


def test_discovered_core_catalog_meets_the_floor() -> None:
    assert len(discover_core_specs()) >= CORE_CATALOG_FLOOR


def test_the_floor_stays_close_enough_to_the_shipped_count_to_catch_a_small_loss() -> None:
    # A floor far below the live count is the shape that let 243 -> 241 pass unseen.
    assert len(discover_core_specs()) - CORE_CATALOG_FLOOR < 10


def test_discovery_raises_when_scenarios_dir_is_missing(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setattr(discovery, "SCENARIOS_DIR", tmp_path / "does-not-exist")
    with pytest.raises(ScenarioCatalogError):
        discover_specs()
