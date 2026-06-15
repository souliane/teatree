"""The scenario catalog cannot silently shrink — a count-floor + an existence guard.

A mis-pointed move (off-by-one dir, a typo'd ``SCENARIOS_DIR``) makes
``SCENARIOS_DIR.glob("*.yaml")`` return ``[]`` without raising, so a metered
``run --backend sdk`` would execute only the handful of co-located specs, meter
``>$0``, and exit GREEN — ``assert_executed_when_required`` only fires on
``executed == 0``. Two deterministic guards close that hole:

*   a floor: the discovered core catalog is large (≥63), so a collapse to a
    handful is a hard RED here, not a quiet shrink;
*   an existence check: a missing ``SCENARIOS_DIR`` raises rather than yielding
    an empty catalog.
"""

import pytest

from teatree.eval import discovery
from teatree.eval.discovery import ScenarioCatalogError, discover_specs

#: The catalog has well over this many specs today (179 at the time of writing);
#: the floor is a deliberately conservative collapse-detector, not an exact count.
_CATALOG_FLOOR = 63


def test_discovered_catalog_meets_the_floor() -> None:
    assert len(discover_specs()) >= _CATALOG_FLOOR


def test_discovery_raises_when_scenarios_dir_is_missing(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setattr(discovery, "SCENARIOS_DIR", tmp_path / "does-not-exist")
    with pytest.raises(ScenarioCatalogError):
        discover_specs()
