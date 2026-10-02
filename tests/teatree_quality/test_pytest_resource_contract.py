"""A bare local pytest invocation must stay bounded and scoped."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

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


@pytest.mark.timeout(240)
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
        timeout=200,
        check=False,
    )
    assert "unsharded whole-tree pytest is refused" not in result.stderr
