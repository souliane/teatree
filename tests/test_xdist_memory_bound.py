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

from tests._git_repo import make_git_repo

_BASH = shutil.which("bash") or "/bin/bash"
_REPO_ROOT = Path(__file__).resolve().parents[1]
_HELPER = _REPO_ROOT / "dev" / "lib" / "xdist-workers.sh"

_MIB = 1024 * 1024
_GIB = 1024 * _MIB

# The lanes that run pytest and therefore must bound their own worker pool.
_PYTEST_LANES = ("dev/push-gate.sh", "dev/test-affected.sh", "dev/test-cov.sh")

# The helper's record of the bound it chose, read back by the push refusal in
# `teatree.core.forge_push`. Both sides resolve it from the git COMMON dir.
_BREADCRUMB = "t3-xdist-bound"


def _invoke(
    *,
    v2_contents: str | None,
    env: dict[str, str] | None = None,
    tmp_path: Path,
    cores: int = 16,
    cwd: Path | None = None,
) -> tuple[str, str]:
    """Source the helper against a FAKE cgroup file and report the resulting cap.

    *cores* is pinned rather than detected: the bound only applies when memory is the
    BINDING constraint, so a test that let the runner's real core count decide would
    assert something different on a 2-core runner than on a 16-core one — the same
    environment coupling these changes exist to remove.
    """
    v2 = tmp_path / "memory.max"
    if v2_contents is not None:
        v2.write_text(v2_contents, encoding="utf-8")
    script = (
        f"set -euo pipefail\n"
        f'export T3_CGROUP_MEMORY_MAX_V2="{v2}"\n'
        f'export T3_CGROUP_MEMORY_MAX_V1="{tmp_path / "absent-v1"}"\n'
        f'export T3_CPU_COUNT="{cores}"\n'
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
    # The two budget knobs are pinned ahead of the case's own env so an ambient value cannot
    # decide the arithmetic, while a case that is ABOUT one of them can still override it.
    pinned = {"T3_MB_PER_TEST_WORKER": "512", "T3_MB_PARENT_RESERVE": "512"}
    completed = subprocess.run(
        [_BASH, "-c", script],
        capture_output=True,
        text=True,
        env={**base, **pinned, **(env or {})},
        cwd=cwd,
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
        # A 2 GiB cap at 512 MiB/worker leaves (2048-512)/512 = 3 workers once the parent's
        # own 512 MiB is set aside. Budgeting all 4 is what OOM-killed the push gate (#4589).
        workers, output = _invoke(v2_contents=str(2 * _GIB), tmp_path=tmp_path)

        assert workers == "3", f"expected a memory-derived bound of 3 workers, got {workers!r}"
        assert "cgroup memory cap" in output, "the bound must SAY why it applied — an unexplained cap is a mystery"
        assert "reserve" in output, "the bound must name the reserve it withheld, or its arithmetic is unreadable"

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

    @pytest.mark.parametrize("cap_mib", [2048, 3072, 4096, 8192])
    def test_the_bound_reserves_headroom_for_the_pytest_parent(self, cap_mib: int, tmp_path: Path) -> None:
        """Workers plus the parent's reserve must FIT the cap, with cores never the binding term.

        Budgeting the whole cap to workers leaves the pytest parent, Django's per-worker
        import and the container floor with nothing, so the run overshoots and the cgroup
        kills it — measured at 2050 MiB peak against a 2048 MiB cap (#4589).
        """
        workers, _ = _invoke(v2_contents=str(cap_mib * _MIB), tmp_path=tmp_path, cores=20)

        assert int(workers) * 512 + 512 <= cap_mib, (
            f"a {cap_mib} MiB cap budgeted {workers} workers at 512 MiB plus a 512 MiB reserve, "
            "which does not fit — the pool is over the cap before pytest has started"
        )

    def test_a_cap_that_cannot_afford_one_worker_says_so_loudly(self, tmp_path: Path) -> None:
        """Under budget, 1 worker is the least-bad answer — but silently hoping is not.

        600 MiB less the 512 MiB reserve leaves 88 MiB, a sixth of one worker. The lane still
        needs a runner, so it gets one; what it must not do is present that as a bound that fits.
        """
        workers, output = _invoke(v2_contents=str(600 * _MIB), tmp_path=tmp_path)

        assert workers == "1"
        assert "WARNING" in output, "an over-budget run must announce itself, not clamp to 1 in silence"
        assert "600" in output, "the warning must name the cap, or it is not actionable"
        assert "512" in output, "the warning must name the reserve it could not afford a worker under"

    def test_a_cap_below_the_reserve_still_yields_one_worker(self, tmp_path: Path) -> None:
        # 256 MiB is less than the reserve alone, so the budget goes negative before dividing.
        workers, output = _invoke(v2_contents=str(256 * _MIB), tmp_path=tmp_path)

        assert workers == "1"
        assert "WARNING" in output

    @pytest.mark.parametrize(("reserve", "expected"), [("0", "4"), ("1024", "2")])
    def test_the_reserve_is_overridable(self, reserve: str, expected: str, tmp_path: Path) -> None:
        # The knob has to be live, or a host whose parent costs more than 512 MiB cannot be tuned
        # without editing the helper. Reserving nothing reproduces the pre-#4589 arithmetic.
        workers, _ = _invoke(
            v2_contents=str(2 * _GIB),
            env={"T3_MB_PARENT_RESERVE": reserve},
            tmp_path=tmp_path,
        )

        assert workers == expected

    def test_the_reserve_can_make_memory_the_binding_constraint(self, tmp_path: Path) -> None:
        """A cap that cleared the core count before the reserve may not clear it after.

        8 cores against 4 GiB allowed exactly 8 workers, so the helper stood down and `-n auto`
        took the whole box — at 100% of the cap. The reserve is what makes memory bind here.
        """
        workers, _ = _invoke(v2_contents=str(4 * _GIB), tmp_path=tmp_path, cores=8)

        assert workers == "7"

    def test_a_tiny_cap_still_yields_at_least_one_worker(self, tmp_path: Path) -> None:
        # A cap below one worker's footprint must degrade to a serial-ish run, not to 0
        # (which xdist reads as "no workers" and which would wedge the lane entirely).
        workers, _ = _invoke(v2_contents=str(100 * 1024 * 1024), tmp_path=tmp_path)

        assert workers == "1"


class TestTheBoundIsLegibleAfterTheLaneIsKilled:
    """An OOM-killed lane takes its own buffered output with it, so the bound must outlive it.

    `t3 push` reports a gate that "refused and printed nothing" and correctly SUSPECTS an OOM
    cap — but a suspicion is not a diagnosis. The helper leaves the numbers it chose beside the
    hook that ran it, so the refusal can name them instead of guessing (#4589).
    """

    def test_the_bound_leaves_a_breadcrumb_beside_the_pre_push_hook(self, tmp_path: Path) -> None:
        repo = make_git_repo(tmp_path / "repo")

        _invoke(v2_contents=str(2 * _GIB), tmp_path=tmp_path, cwd=repo)

        breadcrumb = repo / ".git" / _BREADCRUMB
        assert breadcrumb.is_file(), (
            f"the helper must record its bound at {_BREADCRUMB} in the git common dir — the "
            "refusal message reads it from beside the pre-push hook, which lives there too"
        )
        recorded = breadcrumb.read_text(encoding="utf-8")
        assert "workers=3" in recorded
        assert "cap_mib=2048" in recorded

    def test_a_cwd_outside_a_git_repo_is_not_an_error(self, tmp_path: Path) -> None:
        # The helper only ADVISES a lane; failing one because there is nowhere to drop a
        # diagnostic would turn a missing breadcrumb into a broken test run.
        outside = tmp_path / "not-a-repo"
        outside.mkdir()

        workers, _ = _invoke(v2_contents=str(2 * _GIB), tmp_path=tmp_path, cwd=outside)

        assert workers == "3"


@pytest.mark.parametrize("lane", _PYTEST_LANES)
def test_pytest_lane_bounds_its_worker_pool(lane: str) -> None:
    body = (_REPO_ROOT / lane).read_text(encoding="utf-8")

    reason = (
        f"{lane} runs pytest but never bounds its worker pool by the container's memory cap, "
        "so on a memory-tight box it dies as an opaque xdist crash instead of running bounded."
    )
    assert "xdist-workers.sh" in body, reason
    assert "bound_xdist_workers_to_memory" in body, reason


def test_cap_above_the_core_count_leaves_auto_detection_alone(tmp_path: Path) -> None:
    """A cap that allows MORE workers than there are cores is not the binding constraint.

    Bounding there would cut the pool below what `-n auto` would rightly pick, so the
    helper must stand down. This branch is why the bound cannot be asserted against a
    fixed number without pinning the core count too.
    """
    workers, _ = _invoke(v2_contents=str(64 * _GIB), tmp_path=tmp_path, cores=2)

    assert workers == "unset"
