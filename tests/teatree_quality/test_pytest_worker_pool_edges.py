"""Failure and environment boundaries of the shared worker pool."""

import errno
import fcntl
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from teatree.quality.pytest_resource_contract import (
    INHERITED_LEASE_ENV,
    TOTAL_TEST_WORKERS_PER_CORE,
    PoolUnavailableError,
    WorkerSlotPool,
)
from teatree.utils.ram_probe import available_cpu_count
from tests.teatree_quality.real_pytest_pool_support import REPO, pytest_command, standalone_pytest_env


def test_stale_inherited_lease_does_not_skip_a_new_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pool = WorkerSlotPool(tmp_path, capacity=2)
    with pool.acquire(1):
        monkeypatch.setenv(INHERITED_LEASE_ENV, f"{pool.path}:99999999")
        with pool.session_lease(2) as lease:
            assert lease.workers == 1
            assert os.environ[INHERITED_LEASE_ENV] == f"{pool.path}:{os.getpid()}"
    assert INHERITED_LEASE_ENV not in os.environ


def test_flock_error_closes_the_opened_slot_descriptor(tmp_path: Path) -> None:
    original_flock = fcntl.flock
    before = len(list(Path("/proc/self/fd").iterdir()))

    def fail_slot_lock(descriptor: int, operation: int) -> None:
        if (Path("/proc/self/fd") / str(descriptor)).readlink().name == "slot-0000.lock":
            raise OSError(errno.EIO, "slot lock failed")
        original_flock(descriptor, operation)

    with (
        patch("teatree.quality.pytest_resource_contract.fcntl.flock", side_effect=fail_slot_lock),
        pytest.raises(PoolUnavailableError, match=r"slot-0000\.lock"),
    ):
        WorkerSlotPool(tmp_path, capacity=1).acquire(1)
    assert len(list(Path("/proc/self/fd").iterdir())) == before


def test_standalone_pytest_drops_ambient_auto_worker_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PYTEST_XDIST_AUTO_NUM_WORKERS", "99")
    assert "PYTEST_XDIST_AUTO_NUM_WORKERS" not in standalone_pytest_env(tmp_path)


@pytest.mark.timeout(60)
def test_real_collect_only_run_takes_no_slot(tmp_path: Path) -> None:
    node = (
        f"{REPO}/tests/teatree_quality/test_pytest_resource_contract.py"
        "::test_auto_workers_are_parallel_but_bounded_on_a_healthy_box"
    )
    capacity = available_cpu_count() * TOTAL_TEST_WORKERS_PER_CORE
    with WorkerSlotPool(tmp_path, capacity=capacity).acquire(capacity):
        result = subprocess.run(
            pytest_command("-n", "3", "--collect-only", "-q", node),
            cwd=REPO,
            env=standalone_pytest_env(tmp_path),
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
    assert result.returncode == 0, result.stderr
    assert "requested workers" not in result.stderr


@pytest.mark.timeout(60)
def test_registry_override_never_leaks_into_hermetic_tests(tmp_path: Path) -> None:
    node = (
        f"{REPO}/tests/teatree_quality/test_pytest_resource_contract.py"
        "::test_hermetic_tests_resolve_registries_under_the_sandboxed_home"
    )
    result = subprocess.run(
        pytest_command("-n", "0", "-q", node),
        cwd=REPO,
        env=standalone_pytest_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.timeout(60)
@pytest.mark.parametrize("capacity_state", ["corrupt", "unreadable"])
def test_real_pytest_rebuilds_an_unusable_capacity_file(tmp_path: Path, capacity_state: str) -> None:
    slot_dir = tmp_path / "pytest-worker-slots"
    slot_dir.mkdir()
    capacity_file = slot_dir / "capacity.lock"
    capacity_file.write_text("corrupt" if capacity_state == "corrupt" else "3", encoding="utf-8")
    if capacity_state == "unreadable":
        capacity_file.chmod(0)
    node = (
        f"{REPO}/tests/teatree_quality/test_pytest_resource_contract.py"
        "::test_auto_workers_are_parallel_but_bounded_on_a_healthy_box"
    )
    result = subprocess.run(
        pytest_command("-n", "1", "-q", node),
        cwd=REPO,
        env=standalone_pytest_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert capacity_file.read_text(encoding="utf-8").strip() == str(available_cpu_count() * TOTAL_TEST_WORKERS_PER_CORE)


def test_worker_pool_docs_describe_current_behavior() -> None:
    blueprint = (REPO / "docs/blueprint/admission-governor.md").read_text(encoding="utf-8")
    pool_section = blueprint.split("**Shared pytest worker slots", 1)[1].split("\n\n", 1)[0]
    governor = (REPO / "src/teatree/core/admission_governor.py").read_text(encoding="utf-8")
    assert "TEATREE_PYTEST_WORKER_POOL_ROOT" in pool_section
    assert "capacity.lock" in pool_section
    assert "first pull request" not in pool_section
    assert "live holder guard" not in blueprint
    assert "formerly" not in governor
