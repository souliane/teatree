"""A bare local pytest invocation must stay bounded and scoped."""

import dataclasses
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from teatree.quality.pytest_resource_contract import bounded_auto_workers, local_run_is_scoped, whole_tree_refusal
from tests import conftest as root_conftest


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


class TestLocalRunIsScoped:
    """`tests/conftest.py::pytest_sessionstart` delegates its ``sharded`` decision here.

    A ``--tach``-scoped run (the ``dev/test-affected.sh`` SCOPED shape) names
    ``tests`` as a collection root whenever a changed src module needs
    ``--doctest-modules`` parity (``Selection.pytest_args``) — the tach plugin's own
    ``pytest_collection_modifyitems`` then deselects everything the diff cannot
    reach. Refusing that as an unbounded whole-tree run would break the documented
    local default lane on every ticket that touches ``src/`` — a real regression
    reproduced against the live guard before this fix (see the module docstring's
    sibling `whole_tree_refusal` tests for the mechanical half; this covers the
    ``sharded`` decision the conftest hook now delegates instead of computing inline).
    """

    def test_tach_flag_alone_counts_as_scoped(self) -> None:
        assert local_run_is_scoped(splits=0, group=0, tach=True, ci_node_total=None, ci_node_index=None) is True

    def test_ci_pytest_split_pair_counts_as_scoped(self) -> None:
        assert local_run_is_scoped(splits=4, group=1, tach=False, ci_node_total=None, ci_node_index=None) is True

    def test_ci_node_env_pair_counts_as_scoped(self) -> None:
        assert local_run_is_scoped(splits=0, group=0, tach=False, ci_node_total="4", ci_node_index="1") is True

    def test_a_bare_local_invocation_is_not_scoped(self) -> None:
        assert local_run_is_scoped(splits=0, group=0, tach=False, ci_node_total=None, ci_node_index=None) is False

    def test_one_half_of_a_pair_alone_does_not_count(self) -> None:
        assert local_run_is_scoped(splits=4, group=0, tach=False, ci_node_total=None, ci_node_index=None) is False
        assert local_run_is_scoped(splits=0, group=0, tach=False, ci_node_total="4", ci_node_index=None) is False


@dataclasses.dataclass
class _FakeOption:
    tach: bool
    splits: int = 0
    group: int = 0


class _FakeConfig:
    """The subset of ``pytest.Config`` the hook reads — no real collection needed."""

    def __init__(self, *, args: list[str], root: Path, tach: bool) -> None:
        self.args = args
        self.rootpath = root
        self.option = _FakeOption(tach=tach)


def test_sessionstart_does_not_raise_for_a_tach_scoped_tests_root(tmp_path: Path) -> None:
    """The real regression: a `--tach` run naming `tests` used to be refused anyway.

    Calls the actual hook (not just the extracted pure function) with a fake
    config carrying the exact shape `dev/test-affected.sh` emits — bare `tests`
    root, `tach=True` — so the wiring is proven, not just `local_run_is_scoped`'s
    own logic. No real collection: the hook raises (or doesn't) before pytest
    ever imports a test module.
    """
    config = _FakeConfig(args=["tests", "src/teatree/quality/pytest_resource_contract.py"], root=tmp_path, tach=True)
    session = SimpleNamespace(config=config)
    root_conftest.pytest_sessionstart(session)  # must not raise


def test_sessionstart_still_raises_for_an_unsharded_tests_root(tmp_path: Path) -> None:
    config = _FakeConfig(args=["tests"], root=tmp_path, tach=False)
    session = SimpleNamespace(config=config)
    with pytest.raises(pytest.UsageError, match="unsharded whole-tree pytest is refused"):
        root_conftest.pytest_sessionstart(session)
