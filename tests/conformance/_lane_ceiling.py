"""The wall-clock ceiling this lane's items run under.

The project-wide ``timeout`` in ``pyproject.toml`` is sized for an ordinary unit
test: a fixed amount of work whose cost does not move when the repo grows. Every
item in this package is the other kind — its INPUT is the whole tree, which is
why ``dev/push-gate.sh`` keeps the lane on the push path at all (a diff-scoped
selector cannot decide a whole-tree assertion is unaffected). Parsing
``src/teatree`` alone measured 14.2s at 1694 modules, and the slowest item here
measured 60.02s of call time on a host running other lanes — so the unit-test
ceiling kills a lane that is working correctly, and it does so on a different
item each run, which reads as flakiness rather than as the ceiling being wrong.

The ceiling is raised for this package and BOUNDED: a genuine hang still fails
the push rather than wedging it forever. Same idiom, same value, as the other
whole-tree scans that already carry their own marker
(``tests/teatree_cli/test_cli_reference_drift_gate.py``,
``tests/test_loop_ownership_transfer.py``).
"""

import tomllib
from typing import Protocol

import pytest

from tests.conformance._src_tree import REPO_ROOT

LANE_TIMEOUT_SECONDS = 300

#: A hang must still fail the push. The ceiling buys headroom over the measured
#: worst item; it is not licence to run unbounded.
LANE_TIMEOUT_CAP_SECONDS = 600


class MarkableItem(Protocol):
    def get_closest_marker(self, name: str) -> pytest.Mark | None: ...

    def add_marker(self, marker: pytest.MarkDecorator) -> None: ...


def project_default_timeout_seconds() -> int:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return int(pyproject["tool"]["pytest"]["ini_options"]["timeout"])


def apply_lane_ceiling(items: list[MarkableItem]) -> None:
    for item in items:
        if item.get_closest_marker("timeout") is None:
            item.add_marker(pytest.mark.timeout(LANE_TIMEOUT_SECONDS))
