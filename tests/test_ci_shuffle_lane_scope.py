"""The order-dependence (shuffle) lane audits a curated, order-safe set (#3160 CI-4).

The lane was scoped to ``tests/teatree_loop/`` alone. It is widened to every
additional directory empirically verified order-safe under all four matrixed seeds,
both standalone and shuffled together in one process. This pins the widened set so a
future edit that silently narrows the lane back to the loop dir turns red here, and
keeps the original loop dir (the #2359 Class B reproducer home) in scope.
"""

from pathlib import Path
from typing import Any, cast

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CI_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "ci.yml"

# The directories empirically confirmed order-safe under shuffle (seeds 7/1/13/100),
# both standalone and shuffled together as one -n0 process.
_WIDENED_DIRS = (
    "tests/teatree_loop/",
    "tests/config/",
    "tests/teatree_config/",
    "tests/teatree_utils/",
    "tests/utils/",
    "tests/teatree_quality/",
    "tests/messaging/",
    "tests/cli_doctor/",
    "tests/conformance/",
    "tests/teatree_hooks/",
)


def _shuffle_run() -> str:
    jobs = cast("dict[str, Any]", yaml.safe_load(_CI_WORKFLOW.read_text(encoding="utf-8"))["jobs"])
    steps = [s for s in jobs["test-shuffle"]["steps"] if isinstance(s, dict)]
    run_steps = [str(s.get("run", "")) for s in steps if "randomly" in str(s.get("run", ""))]
    assert run_steps, "test-shuffle must have a step that runs pytest under -p randomly."
    return run_steps[0]


class TestShuffleLaneScope:
    def test_still_includes_the_loop_reproducer_dir(self) -> None:
        assert "tests/teatree_loop/" in _shuffle_run(), (
            "The shuffle lane must keep tests/teatree_loop/ — the #2359 Class B "
            "order-dependence reproducer lives there."
        )

    def test_lane_is_widened_beyond_the_loop_dir(self) -> None:
        run = _shuffle_run()
        for directory in _WIDENED_DIRS:
            assert directory in run, (
                f"The shuffle lane must audit {directory} — it was confirmed order-safe under "
                "all four matrixed seeds (CI-4). A missing dir silently narrows the audit."
            )

    def test_runs_serially_under_shuffle(self) -> None:
        # -n0 (serial) is load-bearing: xdist would isolate a polluter from its victim
        # across workers, defeating the whole order-dependence audit.
        run = _shuffle_run()
        assert "-n0" in run, "The shuffle lane must run serially (-n0) so one in-process order is exercised."
        assert "-p randomly" in run, "The shuffle lane must load pytest-randomly."
