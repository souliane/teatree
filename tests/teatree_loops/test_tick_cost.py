"""The worker tick CPU reading includes its own and waited children's CPU."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from teatree.loops.tick_cost import process_cpu_seconds


def test_process_cpu_includes_waited_children() -> None:
    own = SimpleNamespace(ru_utime=1.0, ru_stime=0.5)
    children = SimpleNamespace(ru_utime=0.25, ru_stime=0.75)
    with (
        patch("teatree.loops.tick_cost.resource.RUSAGE_SELF", 0),
        patch("teatree.loops.tick_cost.resource.RUSAGE_CHILDREN", 1),
        patch("teatree.loops.tick_cost.resource.getrusage", side_effect=lambda who: own if who == 0 else children),
    ):
        assert process_cpu_seconds() == pytest.approx(2.5)
