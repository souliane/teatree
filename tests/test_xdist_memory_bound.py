"""``-n auto`` must be bounded by the container's MEMORY cap, not just its cores.

pytest-xdist sizes ``-n auto`` from the CPU count. Inside a memory-capped
container that count is the HOST's, because a cgroup memory limit does not change
``nproc`` — so a container capped well below ``cores x per-worker RAM`` spawns far
more workers than its memory allows. The run then dies as an opaque xdist "worker
crashed" rather than as the memory limit it actually is, which reads as a flaky
lane instead of a misconfigured one.

``dev/lib/xdist-workers.sh`` closes that: it defaults
``PYTEST_XDIST_AUTO_NUM_WORKERS`` from the cgroup cap when the caller has not
pinned one, so the lane runs bounded instead of crashing, and says so.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

_BASH = shutil.which("bash") or "/bin/bash"
_REPO_ROOT = Path(__file__).resolve().parents[1]
_HELPER = _REPO_ROOT / "dev" / "lib" / "xdist-workers.sh"

_GIB = 1024 * 1024 * 1024

# The lanes that run pytest and therefore must bound their own worker pool.
_PYTEST_LANES = ("dev/push-gate.sh", "dev/test-affected.sh", "dev/test-cov.sh")


def _invoke(*, v2_contents: str | None, env: dict[str, str] | None = None, tmp_path: Path) -> tuple[str, str]:
    """Source the helper against a FAKE cgroup file and report the resulting cap."""
    v2 = tmp_path / "memory.max"
    if v2_contents is not None:
        v2.write_text(v2_contents, encoding="utf-8")
    script = (
        f"set -euo pipefail\n"
        f'export T3_CGROUP_MEMORY_MAX_V2="{v2}"\n'
        f'export T3_CGROUP_MEMORY_MAX_V1="{tmp_path / "absent-v1"}"\n'
        f'. "{_HELPER}"\n'
        f"bound_xdist_workers_to_memory\n"
        f'echo "WORKERS=${{PYTEST_XDIST_AUTO_NUM_WORKERS:-unset}}"\n'
    )
    # Drop any ambient PYTEST_XDIST_AUTO_NUM_WORKERS before applying the case's own
    # env: the runner itself is often invoked with that variable set (bounding the
    # suite on a memory-tight host), and inheriting it would take the helper's
    # "an explicit pin wins" branch in EVERY case — so each cap assertion would read
    # the runner's environment instead of the code, exactly the coupling these
    # changes exist to remove.
    base = {key: value for key, value in os.environ.items() if key != "PYTEST_XDIST_AUTO_NUM_WORKERS"}
    completed = subprocess.run(
        [_BASH, "-c", script],
        capture_output=True,
        text=True,
        env={**base, **(env or {}), "T3_MB_PER_TEST_WORKER": "512"},
        check=True,
    )
    workers = next(
        line.removeprefix("WORKERS=") for line in completed.stdout.splitlines() if line.startswith("WORKERS=")
    )
    return workers, completed.stdout


class TestMemoryDerivedWorkerBound:
    def test_helper_exists(self) -> None:
        assert _HELPER.is_file(), (
            "dev/lib/xdist-workers.sh must exist — without it every pytest lane sizes `-n auto` "
            "from host cores and OOM-crashes inside a memory-capped container."
        )

    def test_low_cap_bounds_the_worker_pool(self, tmp_path: Path) -> None:
        # A 2 GiB cap at 512 MiB/worker allows 4 workers, however many cores the HOST has.
        workers, output = _invoke(v2_contents=str(2 * _GIB), tmp_path=tmp_path)

        assert workers == "4", f"expected a memory-derived bound of 4 workers, got {workers!r}"
        assert "cgroup memory cap" in output, "the bound must SAY why it applied — an unexplained cap is a mystery"

    def test_uncapped_cgroup_leaves_auto_detection_alone(self, tmp_path: Path) -> None:
        # "max" is cgroup v2 for no limit: a full-size box must keep the whole machine.
        workers, _ = _invoke(v2_contents="max", tmp_path=tmp_path)

        assert workers == "unset"

    def test_missing_cgroup_file_leaves_auto_detection_alone(self, tmp_path: Path) -> None:
        workers, _ = _invoke(v2_contents=None, tmp_path=tmp_path)

        assert workers == "unset"

    def test_an_explicitly_pinned_worker_count_wins(self, tmp_path: Path) -> None:
        # The documented override (`PYTEST_XDIST_AUTO_NUM_WORKERS=2 bash dev/...`) must
        # never be silently overridden by the automatic bound.
        workers, _ = _invoke(
            v2_contents=str(2 * _GIB),
            env={"PYTEST_XDIST_AUTO_NUM_WORKERS": "2"},
            tmp_path=tmp_path,
        )

        assert workers == "2"

    def test_a_tiny_cap_still_yields_at_least_one_worker(self, tmp_path: Path) -> None:
        # A cap below one worker's footprint must degrade to a serial-ish run, not to 0
        # (which xdist reads as "no workers" and which would wedge the lane entirely).
        workers, _ = _invoke(v2_contents=str(100 * 1024 * 1024), tmp_path=tmp_path)

        assert workers == "1"


@pytest.mark.parametrize("lane", _PYTEST_LANES)
def test_pytest_lane_bounds_its_worker_pool(lane: str) -> None:
    body = (_REPO_ROOT / lane).read_text(encoding="utf-8")

    reason = (
        f"{lane} runs pytest but never bounds its worker pool by the container's memory cap, "
        "so on a memory-tight box it dies as an opaque xdist crash instead of running bounded."
    )
    assert "xdist-workers.sh" in body, reason
    assert "bound_xdist_workers_to_memory" in body, reason
