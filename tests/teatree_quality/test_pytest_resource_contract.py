"""A bare local pytest invocation must stay bounded and scoped."""

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from teatree.quality.pytest_resource_contract import (
    INHERITED_LEASE_ENV,
    TOTAL_TEST_WORKERS_PER_CORE,
    PoolUnavailableError,
    WorkerSlotPool,
    bounded_auto_workers,
    whole_tree_refusal,
)
from teatree.utils.hook_registry import loop_registry_dir
from teatree.utils.ram_probe import available_cpu_count


def test_auto_workers_are_parallel_but_bounded_on_a_healthy_box() -> None:
    assert bounded_auto_workers(cores=10, memory_mib=8192, explicit=None) == 4
    assert bounded_auto_workers(cores=2, memory_mib=2048, explicit=None) == 2


def test_explicit_ci_worker_budget_is_preserved() -> None:
    assert bounded_auto_workers(cores=10, memory_mib=8192, explicit="6") == 6


def test_whole_tree_guard_refuses_unsharded_local_run(tmp_path: Path) -> None:
    assert "dev/ci-parity-fast.sh" in whole_tree_refusal(["tests"], root=tmp_path, sharded=False)
    assert "PYTEST_XDIST_AUTO_NUM_WORKERS" in whole_tree_refusal([], root=tmp_path, sharded=False)
    assert whole_tree_refusal(["tests/teatree_core/test_admission_governor.py"], root=tmp_path, sharded=False) == ""
    assert whole_tree_refusal(["tests"], root=tmp_path, sharded=True) == ""


def test_explicit_affected_full_run_is_allowed(tmp_path: Path) -> None:
    assert whole_tree_refusal([], root=tmp_path, sharded=False, full_selected=True) == ""
    assert whole_tree_refusal([], root=tmp_path, sharded=False)


def test_tach_scoped_run_is_never_the_whole_tree_it_guards_against(tmp_path: Path) -> None:
    """``SelectionResult.pytest_args`` emits two SCOPED shapes, neither an explicit test id.

    The flags-only shape (no changed src modules) leaves ``config.args`` empty; the
    ``--doctest-modules`` shape passes the literal ``tests`` collection root (so the
    positionals do not clobber ``testpaths``) plus the changed modules. Both resolve to
    "whole tree" under the positional-args heuristic alone — the guard must special-case
    ``tach_active`` (``--tach``'s own parsed option) rather than read either shape as the
    accidental bare invocation it exists to refuse.
    """
    assert whole_tree_refusal([], root=tmp_path, sharded=False, tach_active=True) == ""
    assert (
        whole_tree_refusal(
            ["tests", "src/teatree/config/schema.py", "--doctest-modules"],
            root=tmp_path,
            sharded=False,
            tach_active=True,
        )
        == ""
    )


def test_real_unsharded_pytest_refuses_before_collection() -> None:
    repo = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    for name in ("CI_NODE_TOTAL", "CI_NODE_INDEX", "PYTEST_ADDOPTS"):
        env.pop(name, None)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-n", "0", "--collect-only", "-q"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 4
    assert "unsharded whole-tree pytest is refused" in result.stderr
    assert "collected" not in result.stdout


@pytest.mark.timeout(420)
def test_real_tach_scoped_invocation_is_never_refused() -> None:
    """The exact flags-only shape ``dev/test-affected.sh`` emits on a doc-only diff.

    Reproduces souliane/teatree's own local-default lane end to end: no explicit test
    ids, just ``--tach --tach-base <ref> -p force_keep_plugin`` — the shape a diff with
    zero changed src modules always produces (``SelectionResult.pytest_args``). Before
    the ``tach_active`` carve-out this hit the whole-tree guard on EVERY such run.
    Unlike the refused case above, a real collection runs here (the guard no longer
    short-circuits it), so this is slower than the default per-test timeout affords.
    """
    repo = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    for name in ("CI_NODE_TOTAL", "CI_NODE_INDEX", "PYTEST_ADDOPTS"):
        env.pop(name, None)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-n",
            "0",
            "--tach",
            "--tach-base",
            "origin/main",
            "-p",
            "teatree.quality.force_keep_plugin",
            "--collect-only",
            "-q",
        ],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=360,
        check=False,
    )
    assert "unsharded whole-tree pytest is refused" not in result.stderr


def test_slot_pool_caps_explicit_requests_and_releases(tmp_path: Path) -> None:
    pool = WorkerSlotPool(tmp_path, capacity=3)
    with pool.acquire(9) as lease:
        assert lease.workers == 3
    with pool.acquire(2) as lease:
        assert lease.workers == 2


@pytest.mark.timeout(5)
def test_slot_pool_waits_for_capacity_and_says_why_once(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    pool = WorkerSlotPool(tmp_path, capacity=1)
    first = pool.acquire(1)
    acquired = threading.Event()

    def contender() -> None:
        with pool.acquire(1, poll_interval=0.01):
            acquired.set()

    thread = threading.Thread(target=contender)
    thread.start()
    assert not acquired.wait(0.2)
    first.release()
    assert acquired.wait(1)
    thread.join(timeout=1)
    assert capsys.readouterr().err.count("has no free slots (1 total); waiting") == 1


@pytest.mark.timeout(20)
def test_four_overlapping_runs_never_hold_more_than_the_capacity(tmp_path: Path) -> None:
    pool = WorkerSlotPool(tmp_path, capacity=3)
    guard = threading.Lock()
    live = [0]
    peak = [0]
    granted: list[int] = []

    def run() -> None:
        for _ in range(5):
            with pool.acquire(3, poll_interval=0.005) as lease:
                with guard:
                    live[0] += lease.workers
                    peak[0] = max(peak[0], live[0])
                    granted.append(lease.workers)
                time.sleep(0.01)
                with guard:
                    live[0] -= lease.workers

    runs = [threading.Thread(target=run) for _ in range(4)]
    for thread in runs:
        thread.start()
    for thread in runs:
        thread.join(timeout=15)
    assert len(granted) == 20
    assert peak[0] <= 3


def test_missing_shared_root_refuses_pool(tmp_path: Path) -> None:
    pool = WorkerSlotPool(tmp_path / "missing", capacity=1)
    with pytest.raises(PoolUnavailableError):
        pool.acquire(1)


def test_outer_session_lease_is_inherited_by_its_descendants(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(INHERITED_LEASE_ENV)
    pool = WorkerSlotPool(tmp_path, capacity=2)
    with pool.session_lease(5) as outer:
        assert outer.workers == 2
        assert os.environ[INHERITED_LEASE_ENV] == str(pool.path)
        with pool.session_lease(5) as nested:
            assert nested.workers == 1


def test_hermetic_tests_resolve_registries_under_the_sandboxed_home() -> None:
    assert loop_registry_dir() == Path(os.environ["HOME"]) / ".local" / "share" / "teatree"


def _standalone_pytest_env(shared_root: Path) -> dict[str, str]:
    env = {**os.environ, "T3_LOOP_REGISTRY_DIR": str(shared_root)}
    for name in (INHERITED_LEASE_ENV, "PYTEST_CURRENT_TEST", "CI_NODE_TOTAL", "CI_NODE_INDEX", "PYTEST_ADDOPTS"):
        env.pop(name, None)
    return env


def _pytest_command(*args: str) -> list[str]:
    return [sys.executable, "-m", "pytest", "--no-migrations", *args]


_REPO = Path(__file__).resolve().parents[2]
_BOX_CAPACITY = available_cpu_count() * TOTAL_TEST_WORKERS_PER_CORE
_BOUNDED_NODE = f"{__file__}::test_auto_workers_are_parallel_but_bounded_on_a_healthy_box"


@pytest.mark.timeout(60)
def test_real_pytest_refuses_missing_shared_pool(tmp_path: Path) -> None:
    result = subprocess.run(
        _pytest_command("-n", "1", "-q", _BOUNDED_NODE),
        cwd=_REPO,
        env=_standalone_pytest_env(tmp_path / "missing"),
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    assert result.returncode == 4
    assert "shared pytest worker pool root is missing" in result.stderr


@pytest.mark.timeout(60)
def test_real_pytest_caps_explicit_workers_to_free_slots(tmp_path: Path) -> None:
    held = WorkerSlotPool(tmp_path, capacity=_BOX_CAPACITY).acquire(_BOX_CAPACITY - 1) if _BOX_CAPACITY > 1 else None
    try:
        result = subprocess.run(
            _pytest_command("-n", "3", "-vv", _BOUNDED_NODE),
            cwd=_REPO,
            env=_standalone_pytest_env(tmp_path),
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
    finally:
        if held is not None:
            held.release()
    assert result.returncode == 0, result.stderr
    assert "created: 1/1 worker" in result.stdout


@pytest.mark.timeout(60)
def test_real_nested_pytest_runs_on_its_parents_slot_when_the_pool_is_full(tmp_path: Path) -> None:
    env = {**_standalone_pytest_env(tmp_path), INHERITED_LEASE_ENV: str(tmp_path / "pytest-worker-slots")}
    with WorkerSlotPool(tmp_path, capacity=_BOX_CAPACITY).acquire(_BOX_CAPACITY):
        result = subprocess.run(
            _pytest_command("-n", "3", "-vv", _BOUNDED_NODE),
            cwd=_REPO,
            env=env,
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
    assert result.returncode == 0, result.stderr
    assert "created: 1/1 worker" in result.stdout


@pytest.mark.timeout(60)
def test_real_pytest_waits_for_a_slot_and_says_why(tmp_path: Path) -> None:
    held = WorkerSlotPool(tmp_path, capacity=_BOX_CAPACITY).acquire(_BOX_CAPACITY)
    stderr_path = tmp_path / "stderr.log"
    with stderr_path.open("w") as stderr:
        run = subprocess.Popen(
            _pytest_command("-n", "0", "-q", _BOUNDED_NODE),
            cwd=_REPO,
            env=_standalone_pytest_env(tmp_path),
            stdout=subprocess.DEVNULL,
            stderr=stderr,
        )
    try:
        deadline = time.monotonic() + 30
        while "has no free slots" not in stderr_path.read_text() and time.monotonic() < deadline:
            assert run.poll() is None, stderr_path.read_text()
            time.sleep(0.1)
        assert f"has no free slots ({_BOX_CAPACITY} total); waiting" in stderr_path.read_text()
        held.release()
        assert run.wait(timeout=30) == 0, stderr_path.read_text()
    finally:
        held.release()
        if run.poll() is None:
            run.kill()
            run.wait()


@pytest.mark.timeout(60)
def test_registry_override_never_leaks_into_hermetic_tests(tmp_path: Path) -> None:
    result = subprocess.run(
        _pytest_command(
            "-n", "0", "-q", f"{__file__}::test_hermetic_tests_resolve_registries_under_the_sandboxed_home"
        ),
        cwd=_REPO,
        env=_standalone_pytest_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_unwritable_pool_path_refuses(tmp_path: Path) -> None:
    (tmp_path / "pytest-worker-slots").write_text("occupied")
    with pytest.raises(PoolUnavailableError, match="is unavailable"):
        WorkerSlotPool(tmp_path, capacity=2).acquire(1)


def test_symlink_slot_refuses_instead_of_skipping(tmp_path: Path) -> None:
    slot_dir = tmp_path / "pytest-worker-slots"
    slot_dir.mkdir()
    (slot_dir / "slot-0000.lock").symlink_to(tmp_path / "target")
    with pytest.raises(PoolUnavailableError, match="is unreadable"):
        WorkerSlotPool(tmp_path, capacity=2).acquire(1)
