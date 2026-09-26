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


def test_whole_tree_guard_allows_a_tach_scoped_doctest_selection(tmp_path: Path) -> None:
    # Regression for #4856: `affected_tests.py::pytest_args` emits an explicit
    # `tests` root alongside `--doctest-modules` targets on every SCOPED diff that
    # touches a doctest-bearing src module — the common case. `pytest.Config.args`
    # holds only the leftover positionals (flags like `--tach` are stripped by
    # pytest's own parsing), so this exact positional shape must NOT be misread
    # as an unbounded whole-tree run once the caller reports ``tach_scoped=True``.
    doctest_root_positionals = ["tests", "src/teatree/loop/scanners/pr_sweep.py"]
    assert whole_tree_refusal(doctest_root_positionals, root=tmp_path, sharded=False, tach_scoped=True) == ""
    # Anti-vacuity: the SAME positionals still refuse when tach is not in play —
    # proves the exemption is keyed on ``tach_scoped``, not on the path shape.
    assert whole_tree_refusal(doctest_root_positionals, root=tmp_path, sharded=False) != ""


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
