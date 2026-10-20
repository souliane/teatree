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
from teatree.utils.ram_probe import available_cpu_count
from tests.teatree_quality.real_pytest_pool_support import (
    REPO,
    ProbeSettings,
    created_workers,
    kill_group,
    launch_pytest,
    live_workers,
    pytest_command,
    standalone_pytest_env,
    wait_for,
    worker_pids,
)


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


@pytest.mark.timeout(75)
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


def test_outer_session_lease_is_inherited_by_its_descendants(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(INHERITED_LEASE_ENV, raising=False)
    pool = WorkerSlotPool(tmp_path, capacity=2)
    with pool.session_lease(5) as outer:
        assert outer.workers == 2
        assert os.environ[INHERITED_LEASE_ENV] == f"{pool.path}:{os.getpid()}"
        with pool.session_lease(5) as nested:
            assert nested.workers == 1
    assert INHERITED_LEASE_ENV not in os.environ


_BOX_CAPACITY = available_cpu_count() * TOTAL_TEST_WORKERS_PER_CORE
_REAL_CAPACITY = max(2, _BOX_CAPACITY)
_BOUNDED_NODE = f"{__file__}::test_auto_workers_are_parallel_but_bounded_on_a_healthy_box"


class TestWorkerSlotPool:
    def test_takes_free_slots_up_to_the_request_and_never_more(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        pool = WorkerSlotPool(tmp_path, capacity=3)
        with pool.acquire(9) as lease:
            assert lease.workers == 3
        assert "granted 3 of 9" in capsys.readouterr().err
        with pool.acquire(2) as lease:
            assert lease.workers == 2
        assert capsys.readouterr().err == ""

    @pytest.mark.timeout(5)
    def test_waits_for_one_slot_then_names_the_pool_and_its_holders(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
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
        stderr = capsys.readouterr().err
        assert stderr.count(f"has no free slots (capacity 1); holder pid {os.getpid()}; waiting") == 1
        assert str(pool.path) in stderr

    @pytest.mark.timeout(5)
    def test_release_frees_every_held_slot(self, tmp_path: Path) -> None:
        pool = WorkerSlotPool(tmp_path, capacity=3)
        with pool.acquire(2) as first, pool.acquire(1) as second:
            assert first.workers + second.workers == 3
        with pool.acquire(3, poll_interval=0.01) as released:
            assert released.workers == 3

    @pytest.mark.parametrize("obstruction", ["parent-is-a-file", "slot-is-a-directory"])
    def test_an_unusable_pool_raises_naming_the_path(self, tmp_path: Path, obstruction: str) -> None:
        root = tmp_path / "root"
        path = root / "pytest-worker-slots"
        if obstruction == "parent-is-a-file":
            root.write_text("occupied")
        else:
            path.mkdir(parents=True)
            (path / "slot-0000.lock").mkdir()
        with pytest.raises(PoolUnavailableError, match="shared pytest worker pool") as error:
            WorkerSlotPool(root, capacity=1).acquire(1)
        assert str(path / "slot-0000.lock" if obstruction == "slot-is-a-directory" else path) in str(error.value)


class TestRealPytestRunsDrawFromOneBoxPool:
    @pytest.mark.timeout(300)
    def test_four_overlapping_runs_never_exceed_the_free_slots_and_each_waits_saying_why(
        self, real_pytest_root: Path
    ) -> None:
        tmp_path = real_pytest_root
        pool = WorkerSlotPool(tmp_path, capacity=_REAL_CAPACITY)
        held = [pool.acquire(_REAL_CAPACITY - 2), pool.acquire(2)]
        runs = [
            launch_pytest(tmp_path, tmp_path, f"overlap-{index}", "-n", "4", settings=ProbeSettings(sleep=2))
            for index in range(4)
        ]
        try:
            wait_for(
                lambda: all("; waiting" in stderr.read_text(encoding="utf-8") for _, _, stderr, _ in runs),
                timeout=90,
            )
            for _, _, stderr, _ in runs:
                assert str(pool.path) in stderr.read_text(encoding="utf-8")
                assert f"capacity {_REAL_CAPACITY}" in stderr.read_text(encoding="utf-8")
                assert f"holder pid {os.getpid()}" in stderr.read_text(encoding="utf-8")
            held.pop().release()
            peak = 0
            deadline = time.monotonic() + 240
            while any(run.poll() is None for run, _, _, _ in runs) and time.monotonic() < deadline:
                peak = max(peak, live_workers([pids for _, _, _, pids in runs]))
                time.sleep(0.05)
            assert all(run.wait(timeout=10) == 0 for run, _, _, _ in runs)
            assert 1 <= peak <= 2
            assert all(created_workers(stdout.read_text(encoding="utf-8")) >= 1 for _, stdout, _, _ in runs)
        finally:
            for run, _, _, _ in runs:
                kill_group(run)
            for lease in held:
                lease.release()

    @pytest.mark.timeout(150)
    @pytest.mark.parametrize(
        ("worker_request", "worker_args", "worker_cap"),
        [
            pytest.param("explicit-n", ("-n", "4"), None, id="explicit-n"),
            pytest.param("env-override-auto", ("-n", "auto"), "4", id="env-override-auto"),
        ],
    )
    def test_a_request_above_the_free_slots_is_clamped(
        self, real_pytest_root: Path, worker_request: str, worker_args: tuple[str, ...], worker_cap: str | None
    ) -> None:
        tmp_path = real_pytest_root
        capacity = max(4, _REAL_CAPACITY)
        with WorkerSlotPool(tmp_path, capacity=capacity).acquire(capacity - 2):
            run, stdout, stderr, pids = launch_pytest(
                tmp_path,
                tmp_path,
                worker_request,
                *worker_args,
                settings=ProbeSettings(worker_cap=worker_cap),
            )
            try:
                assert run.wait(timeout=120) == 0, stderr.read_text(encoding="utf-8")
                assert "; waiting" not in stderr.read_text(encoding="utf-8")
                created = created_workers(stdout.read_text(encoding="utf-8"))
                assert created == 2
                assert len(worker_pids(pids)) == created
                assert "granted 2 of 4" in stderr.read_text(encoding="utf-8")
            finally:
                kill_group(run)

    @pytest.mark.timeout(60)
    def test_an_unusable_pool_refuses_to_start_naming_the_path(self, real_pytest_root: Path) -> None:
        tmp_path = real_pytest_root
        root = tmp_path / "blocked"
        root.write_text("occupied")
        run, _, stderr, pids = launch_pytest(tmp_path, root, "unusable", "-n", "1")
        assert run.wait(timeout=45) == 4
        assert str(root / "pytest-worker-slots") in stderr.read_text(encoding="utf-8")
        assert not pids.exists()

    @pytest.mark.timeout(180)
    @pytest.mark.parametrize("outcome", ["passing", "failing", "controller-sigkilled"])
    def test_slots_are_held_while_a_run_lives_and_all_free_after_it_exits(
        self, real_pytest_root: Path, outcome: str
    ) -> None:
        tmp_path = real_pytest_root
        pool = WorkerSlotPool(tmp_path, capacity=_REAL_CAPACITY)
        run, _, stderr, pids = launch_pytest(
            tmp_path, tmp_path, outcome, "-n", "1", settings=ProbeSettings(sleep=3, fail=outcome == "failing")
        )
        try:
            wait_for(lambda: bool(worker_pids(pids)))
            with pool.acquire(_REAL_CAPACITY) as remaining:
                assert remaining.workers == _REAL_CAPACITY - 1
            if outcome == "controller-sigkilled":
                run.kill()
            expected = {"passing": 0, "failing": 1, "controller-sigkilled": -9}[outcome]
            assert run.wait(timeout=120) == expected, stderr.read_text(encoding="utf-8")
            with pool.acquire(_REAL_CAPACITY) as released:
                assert released.workers == _REAL_CAPACITY
        finally:
            kill_group(run)

    @pytest.mark.timeout(60)
    def test_a_run_without_xdist_workers_takes_no_slot(self, real_pytest_root: Path) -> None:
        tmp_path = real_pytest_root
        with WorkerSlotPool(tmp_path, capacity=_REAL_CAPACITY).acquire(_REAL_CAPACITY):
            result = subprocess.run(
                pytest_command("-n", "0", "-q", _BOUNDED_NODE),
                cwd=REPO,
                env=standalone_pytest_env(tmp_path),
                capture_output=True,
                text=True,
                timeout=45,
                check=False,
            )
        assert result.returncode == 0, result.stderr
        assert "; waiting" not in result.stderr

    @pytest.mark.timeout(60)
    def test_a_refused_whole_tree_run_never_waits_for_a_slot(self, real_pytest_root: Path) -> None:
        tmp_path = real_pytest_root
        with WorkerSlotPool(tmp_path, capacity=_REAL_CAPACITY).acquire(_REAL_CAPACITY):
            result = subprocess.run(
                pytest_command("-n", "0", "-q"),
                cwd=REPO,
                env=standalone_pytest_env(tmp_path),
                capture_output=True,
                text=True,
                timeout=45,
                check=False,
            )
        assert result.returncode == 4
        assert "unsharded whole-tree pytest is refused" in result.stderr
        assert "; waiting" not in result.stderr
