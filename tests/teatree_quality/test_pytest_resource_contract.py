"""A bare local pytest invocation must stay bounded and scoped."""

import os
import subprocess
import sys
from pathlib import Path

from teatree.quality.pytest_resource_contract import bounded_auto_workers, whole_tree_refusal


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
