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

import ast
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_BASH = shutil.which("bash") or "/bin/bash"
_REPO_ROOT = Path(__file__).resolve().parents[1]
_HELPER = _REPO_ROOT / "dev" / "lib" / "xdist-workers.sh"
_RAM_SCOPE = _REPO_ROOT / "src" / "teatree" / "utils" / "ram_scope.py"

_MIB = 1024 * 1024
_GIB = 1024 * _MIB

# The lanes that run pytest and therefore must bound their own worker pool.
_PYTEST_LANES = ("dev/push-gate.sh", "dev/test-affected.sh", "dev/test-cov.sh")


def _invoke(
    *,
    v2_contents: str | None,
    v2_usage: tuple[str, str] | None = None,
    v1_contents: str | None = None,
    env: dict[str, str] | None = None,
    tmp_path: Path,
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
    current = tmp_path / "memory.current"
    stat = tmp_path / "memory.stat"
    if v2_usage is not None:
        current.write_text(v2_usage[0], encoding="utf-8")
        stat.write_text(v2_usage[1], encoding="utf-8")
    v1 = tmp_path / "memory.limit_in_bytes"
    if v1_contents is not None:
        v1.write_text(v1_contents, encoding="utf-8")
    script = (
        f"set -euo pipefail\n"
        f'export T3_CGROUP_MEMORY_MAX_V2="{v2}"\n'
        f'export T3_CGROUP_MEMORY_CURRENT_V2="{current}"\n'
        f'export T3_CGROUP_MEMORY_STAT_V2="{stat}"\n'
        f'export T3_CGROUP_MEMORY_MAX_V1="{v1}"\n'
        f'. "{_HELPER}"\n'
        f"bound_xdist_workers_to_memory\n"
        f'echo "WORKERS=${{PYTEST_XDIST_AUTO_NUM_WORKERS:-unset}}"\n'
        f'env | grep "^T3_XDIST_BOUND_SUMMARY="\n'
    )
    # Drop any ambient PYTEST_XDIST_AUTO_NUM_WORKERS before applying the case's own
    # env: the runner itself is often invoked with that variable set (bounding the
    # suite on a memory-tight host), and inheriting it would take the helper's
    # "an explicit pin wins" branch in EVERY case — so each cap assertion would read
    # the runner's environment instead of the code, exactly the coupling these
    # changes exist to remove.
    base = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTEST_XDIST_AUTO_NUM_WORKERS", "T3_MB_PER_TEST_WORKER"}
    }
    # The two budget knobs are pinned ahead of the case's own env so an ambient value cannot
    # decide the arithmetic, while a case that is ABOUT one of them can still override it.
    pinned = {"T3_MB_PARENT_RESERVE": "512", "T3_CPU_COUNT": "16"}
    completed = subprocess.run(
        [_BASH, "-c", script],
        capture_output=True,
        text=True,
        env={**base, **pinned, **(env or {})},
        check=False,
    )
    if completed.returncode != 0:
        return "refused", completed.stdout + completed.stderr
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
        # Measured ~750 MiB/worker, rounded up to 768: (2048-512)/768 = 2.
        workers, output = _invoke(v2_contents=str(2 * _GIB), tmp_path=tmp_path)

        assert workers == "2", f"expected a memory-derived bound of 2 workers, got {workers!r}"
        assert "cgroup headroom" in output, "the bound must SAY why it applied — an unexplained cap is a mystery"
        assert "cap 2048 MiB" in output
        assert "reserve" in output, "the bound must name the reserve it withheld, or its arithmetic is unreadable"

    def test_uncapped_cgroup_leaves_auto_detection_alone(self, tmp_path: Path) -> None:
        # "max" is cgroup v2 for no limit: a full-size box must keep the whole machine.
        workers, _ = _invoke(v2_contents="max", tmp_path=tmp_path)

        assert workers == "unset"

    def test_missing_cgroup_file_leaves_auto_detection_alone(self, tmp_path: Path) -> None:
        workers, _ = _invoke(v2_contents=None, tmp_path=tmp_path)

        assert workers == "unset"

    def test_an_explicit_pin_below_the_headroom_bound_stays_lower(self, tmp_path: Path) -> None:
        workers, _ = _invoke(
            v2_contents=str(2 * _GIB),
            env={"PYTEST_XDIST_AUTO_NUM_WORKERS": "2"},
            tmp_path=tmp_path,
        )

        assert workers == "2"

    def test_headroom_not_the_ceiling_sizes_the_worker_pool(self, tmp_path: Path) -> None:
        workers, output = _invoke(
            v2_contents=str(2 * _GIB),
            v2_usage=(
                str(1280 * _MIB),
                f"inactive_file {512 * _MIB}\nslab_reclaimable {128 * _MIB}\nactive_file {64 * _MIB}\n",
            ),
            tmp_path=tmp_path,
        )

        assert workers == "1"
        assert "headroom 1408 MiB" in output

    def test_an_explicit_pin_is_clamped_to_the_headroom_bound(self, tmp_path: Path) -> None:
        workers, output = _invoke(
            v2_contents=str(2 * _GIB),
            v2_usage=(
                str(1280 * _MIB),
                f"inactive_file {512 * _MIB}\nslab_reclaimable {128 * _MIB}\n",
            ),
            env={"PYTEST_XDIST_AUTO_NUM_WORKERS": "4"},
            tmp_path=tmp_path,
        )

        assert workers == "1"
        assert "WARNING" in output
        assert "explicit pin 4" in output

    def test_cgroup_v1_stays_cap_only(self, tmp_path: Path) -> None:
        workers, output = _invoke(
            v2_contents=None,
            v2_usage=(str(2 * _GIB), "inactive_file 0\nslab_reclaimable 0\n"),
            v1_contents=str(2 * _GIB),
            tmp_path=tmp_path,
        )

        assert workers == "2"
        assert "headroom 2048 MiB" in output

    @pytest.mark.parametrize("cap_mib", [2048, 3072, 4096, 8192])
    def test_the_bound_reserves_headroom_for_the_pytest_parent(self, cap_mib: int, tmp_path: Path) -> None:
        """Workers plus the parent's reserve must FIT the cap, with cores never the binding term.

        Budgeting the whole cap to workers leaves the pytest parent, Django's per-worker
        import and the container floor with nothing, so the run overshoots and the cgroup
        kills it — measured at 2050 MiB peak against a 2048 MiB cap (#4589).
        """
        workers, _ = _invoke(v2_contents=str(cap_mib * _MIB), env={"T3_CPU_COUNT": "20"}, tmp_path=tmp_path)

        assert int(workers) * 768 + 512 <= cap_mib, (
            f"a {cap_mib} MiB cap budgeted {workers} workers at 768 MiB plus a 512 MiB reserve, "
            "which does not fit — the pool is over the cap before pytest has started"
        )

    def test_a_cap_that_cannot_afford_one_worker_says_so_loudly(self, tmp_path: Path) -> None:
        """Under budget, refuse explicitly instead of launching a doomed worker."""
        workers, output = _invoke(v2_contents=str(600 * _MIB), tmp_path=tmp_path)

        assert workers == "refused"
        assert "REFUSED pytest" in output
        assert "600" in output, "the warning must name the cap, or it is not actionable"
        assert "768" in output, "the refusal must name the measured worker footprint"

    def test_a_cap_below_the_reserve_refuses(self, tmp_path: Path) -> None:
        # 256 MiB is less than the reserve alone, so the budget goes negative before dividing.
        workers, output = _invoke(v2_contents=str(256 * _MIB), tmp_path=tmp_path)

        assert workers == "refused"
        assert "REFUSED pytest" in output

    @pytest.mark.parametrize(("reserve", "expected"), [("0", "2"), ("1024", "1")])
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
        workers, _ = _invoke(v2_contents=str(4 * _GIB), env={"T3_CPU_COUNT": "8"}, tmp_path=tmp_path)

        assert workers == "4"

    def test_a_tiny_cap_refuses_before_starting_workers(self, tmp_path: Path) -> None:
        workers, _ = _invoke(v2_contents=str(100 * 1024 * 1024), tmp_path=tmp_path)

        assert workers == "refused"


@pytest.mark.parametrize(
    ("v2_contents", "expected"),
    [(None, "cap_mib=uncapped"), ("max", "cap_mib=uncapped"), (str(2 * _GIB), "cap_mib=2048")],
)
def test_bound_summary_is_exported_on_every_cap_path(v2_contents: str | None, expected: str, tmp_path: Path) -> None:
    _, output = _invoke(v2_contents=v2_contents, tmp_path=tmp_path)

    summary = next(line for line in output.splitlines() if line.startswith("T3_XDIST_BOUND_SUMMARY="))
    assert expected in summary
    assert "workers=" in summary
    assert "headroom_mib=" in summary
    assert "reserve_mib=512" in summary
    assert "per_worker_mib=768" in summary


def test_reclaimable_memory_keys_match_ram_scope() -> None:
    shell = _HELPER.read_text(encoding="utf-8")
    match = re.search(r'^_T3_RECLAIMABLE_STAT_KEYS="([^"]+)"$', shell, re.MULTILINE)
    assert match
    shell_keys = frozenset(match.group(1).split())

    tree = ast.parse(_RAM_SCOPE.read_text(encoding="utf-8"))
    assignment = next(
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "_RECLAIMABLE_STAT_KEYS" for target in node.targets)
    )
    assert isinstance(assignment.value, ast.Call)
    python_keys = frozenset(ast.literal_eval(assignment.value.args[0]))
    assert shell_keys == python_keys == frozenset({"inactive_file", "slab_reclaimable"})


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
    workers, _ = _invoke(v2_contents=str(64 * _GIB), env={"T3_CPU_COUNT": "2"}, tmp_path=tmp_path)

    assert workers == "unset"
