"""Package-scoped wiring for the conformance lane.

The ceiling is applied here rather than as a per-file marker so a conformance
test added tomorrow inherits it — a per-file marker is exactly the thing the
next author forgets, and the symptom is a killed push blamed on flakiness.
"""

import pytest

from tests.conformance._lane_ceiling import apply_lane_ceiling


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    apply_lane_ceiling(items)
