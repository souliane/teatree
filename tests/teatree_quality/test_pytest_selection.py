"""Local pytest selections stay scoped and honor recorded durations."""

import json
import os
import subprocess
from pathlib import Path

import pytest

from teatree.quality.durations_file import DurationsUnreadableError
from teatree.quality.pytest_selection import SelectionContext, ci_environment_enabled, oversized_selection_refusal
from tests.teatree_quality.real_pytest_pool_support import REPO, pytest_command


@pytest.mark.parametrize("value", [None, "", " ", "0", "false", "FALSE", "no", "off"])
def test_false_ci_values_keep_the_local_budget(value: str | None) -> None:
    assert not ci_environment_enabled(value)


@pytest.mark.parametrize("value", ["1", "true", "yes"])
def test_true_ci_values_exempt_the_local_budget(value: str) -> None:
    assert ci_environment_enabled(value)


def test_whole_tree_guard_refuses_unsharded_local_run(tmp_path: Path) -> None:
    assert "dev/ci-parity-fast.sh" in oversized_selection_refusal(
        ["tests"], context=SelectionContext(root=tmp_path, sharded=False)
    )
    assert "PYTEST_XDIST_AUTO_NUM_WORKERS" in oversized_selection_refusal(
        [], context=SelectionContext(root=tmp_path, sharded=False)
    )
    assert (
        oversized_selection_refusal(
            ["tests/teatree_core/test_admission_governor.py"], context=SelectionContext(root=tmp_path, sharded=False)
        )
        == ""
    )
    assert oversized_selection_refusal(["tests"], context=SelectionContext(root=tmp_path, sharded=True)) == ""


def test_large_directory_selection_is_refused_before_collection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CI", raising=False)
    package = tmp_path / "tests" / "teatree_core"
    package.mkdir(parents=True)
    (tmp_path / "dev").mkdir()
    (tmp_path / "dev" / ".test_durations").write_text(
        json.dumps({"tests/teatree_core/test_one.py::test_case": 1201.0}),
        encoding="utf-8",
    )

    refusal = oversized_selection_refusal(
        ["tests/teatree_core", "tests/test_ensure_pr_installed_t3_hook.py"],
        context=SelectionContext(root=tmp_path, sharded=False),
    )
    assert "bash dev/test-affected.sh" in refusal
    assert "1201" in refusal
    assert "--teatree-selected-full" in refusal


def test_small_directory_and_file_selections_remain_allowed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CI", raising=False)
    package = tmp_path / "tests" / "small"
    package.mkdir(parents=True)
    (tmp_path / "dev").mkdir()
    (tmp_path / "dev" / ".test_durations").write_text(
        json.dumps({"tests/small/test_one.py::test_case": 1199.0}),
        encoding="utf-8",
    )

    assert oversized_selection_refusal(["tests/small"], context=SelectionContext(root=tmp_path, sharded=False)) == ""
    assert (
        oversized_selection_refusal(["tests/small/test_one.py"], context=SelectionContext(root=tmp_path, sharded=False))
        == ""
    )
    assert (
        oversized_selection_refusal(
            ["tests/small/test_one.py::test_case"], context=SelectionContext(root=tmp_path, sharded=False)
        )
        == ""
    )


def test_large_directory_exemptions_preserve_curated_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CI", raising=False)
    package = tmp_path / "tests" / "large"
    package.mkdir(parents=True)
    (tmp_path / "dev").mkdir()
    (tmp_path / "dev" / ".test_durations").write_text(
        json.dumps({"tests/large/test_one.py::test_case": 1201.0}),
        encoding="utf-8",
    )
    args = ["tests/large"]

    assert oversized_selection_refusal(args, context=SelectionContext(root=tmp_path, sharded=False))
    assert oversized_selection_refusal(args, context=SelectionContext(root=tmp_path, sharded=True)) == ""
    assert (
        oversized_selection_refusal(args, context=SelectionContext(root=tmp_path, sharded=False, tach_active=True))
        == ""
    )
    assert (
        oversized_selection_refusal(args, context=SelectionContext(root=tmp_path, sharded=False, full_selected=True))
        == ""
    )
    assert oversized_selection_refusal(args, context=SelectionContext(root=tmp_path, sharded=False, in_ci=True)) == ""
    assert oversized_selection_refusal(args, context=SelectionContext(root=tmp_path, sharded=False, in_ci=False))


def test_relative_directory_is_resolved_from_invocation_dir(tmp_path: Path) -> None:
    package = tmp_path / "tests" / "large"
    package.mkdir(parents=True)
    (tmp_path / "dev").mkdir()
    (tmp_path / "dev" / ".test_durations").write_text(
        json.dumps({"tests/large/test_one.py::test_case": 1201.0}), encoding="utf-8"
    )

    assert "1201s" in oversized_selection_refusal(
        ["large"], context=SelectionContext(root=tmp_path, invocation_dir=tmp_path / "tests", sharded=False)
    )


def test_unrecorded_directory_is_refused_when_other_durations_exist(tmp_path: Path) -> None:
    (tmp_path / "tests" / "new").mkdir(parents=True)
    (tmp_path / "dev").mkdir()
    (tmp_path / "dev" / ".test_durations").write_text(
        json.dumps({"tests/old/test_one.py::test_case": 1.0}), encoding="utf-8"
    )

    assert "no recorded durations" in oversized_selection_refusal(
        ["tests/new"], context=SelectionContext(root=tmp_path, sharded=False)
    )


def test_overlapping_directory_selections_count_recorded_nodes_once(tmp_path: Path) -> None:
    (tmp_path / "tests" / "many" / "nested").mkdir(parents=True)
    (tmp_path / "dev").mkdir()
    (tmp_path / "dev" / ".test_durations").write_text(
        json.dumps({"tests/many/nested/test_one.py::test_case": 700}), encoding="utf-8"
    )
    assert (
        oversized_selection_refusal(
            ["tests/many", "tests/many/nested"], context=SelectionContext(root=tmp_path, sharded=False)
        )
        == ""
    )


def test_multiple_directory_selections_share_one_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CI", raising=False)
    first = tmp_path / "tests" / "first"
    second = tmp_path / "tests" / "second"
    first.mkdir(parents=True)
    second.mkdir()
    (tmp_path / "dev").mkdir()
    (tmp_path / "dev" / ".test_durations").write_text(
        json.dumps({"tests/first/test_a.py::test_a": 700, "tests/second/test_b.py::test_b": 700}),
        encoding="utf-8",
    )

    refusal = oversized_selection_refusal(
        ["tests/first", "tests/second"], context=SelectionContext(root=tmp_path, sharded=False)
    )
    assert "1400s" in refusal


def test_missing_durations_keeps_existing_path_guard(tmp_path: Path) -> None:
    (tmp_path / "tests" / "small").mkdir(parents=True)
    assert oversized_selection_refusal(["tests/small"], context=SelectionContext(root=tmp_path, sharded=False)) == ""
    assert "unsharded whole-tree" in oversized_selection_refusal(
        ["tests"], context=SelectionContext(root=tmp_path, sharded=False)
    )
    assert "unsharded whole-tree" in oversized_selection_refusal(
        ["tests"], context=SelectionContext(root=tmp_path, sharded=False, in_ci=True)
    )


def test_corrupt_durations_refuses_instead_of_allowing_a_large_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CI", raising=False)
    (tmp_path / "tests" / "large").mkdir(parents=True)
    (tmp_path / "dev").mkdir()
    (tmp_path / "dev" / ".test_durations").write_text("{", encoding="utf-8")

    with pytest.raises(DurationsUnreadableError, match="could not be read"):
        oversized_selection_refusal(["tests/large"], context=SelectionContext(root=tmp_path, sharded=False))


@pytest.mark.parametrize("seconds", [-1.0, float("nan")])
def test_invalid_recorded_seconds_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seconds: float) -> None:
    monkeypatch.delenv("CI", raising=False)
    (tmp_path / "tests" / "large").mkdir(parents=True)
    (tmp_path / "dev").mkdir()
    (tmp_path / "dev" / ".test_durations").write_text(
        json.dumps({"tests/large/test_one.py::test_case": seconds}),
        encoding="utf-8",
    )

    with pytest.raises(DurationsUnreadableError, match="invalid recorded duration"):
        oversized_selection_refusal(["tests/large"], context=SelectionContext(root=tmp_path, sharded=False))


def test_explicit_affected_full_run_is_allowed(tmp_path: Path) -> None:
    assert (
        oversized_selection_refusal([], context=SelectionContext(root=tmp_path, sharded=False, full_selected=True))
        == ""
    )
    assert oversized_selection_refusal([], context=SelectionContext(root=tmp_path, sharded=False))


def test_tach_scoped_run_is_never_the_whole_tree_it_guards_against(tmp_path: Path) -> None:
    """``SelectionResult.pytest_args`` emits two SCOPED shapes, neither an explicit test id.

    The flags-only shape (no changed src modules) leaves ``config.args`` empty; the
    ``--doctest-modules`` shape passes the literal ``tests`` collection root (so the
    positionals do not clobber ``testpaths``) plus the changed modules. Both resolve to
    "whole tree" under the positional-args heuristic alone — the guard must special-case
    ``tach_active`` (``--tach``'s own parsed option) rather than read either shape as the
    accidental bare invocation it exists to refuse.
    """
    assert (
        oversized_selection_refusal([], context=SelectionContext(root=tmp_path, sharded=False, tach_active=True)) == ""
    )
    assert (
        oversized_selection_refusal(
            ["tests", "src/teatree/config/schema.py", "--doctest-modules"],
            context=SelectionContext(root=tmp_path, sharded=False, tach_active=True),
        )
        == ""
    )


@pytest.mark.timeout(75)
def test_real_unsharded_pytest_refuses_before_collection() -> None:
    repo = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    for name in ("CI", "CI_NODE_TOTAL", "CI_NODE_INDEX", "PYTEST_ADDOPTS"):
        env.pop(name, None)
    result = subprocess.run(
        pytest_command("-n", "0", "--collect-only", "-q"),
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


@pytest.mark.timeout(120)
def test_real_pytest_refuses_large_directory_before_collection() -> None:
    env = os.environ.copy()
    for name in ("CI", "CI_NODE_TOTAL", "CI_NODE_INDEX", "PYTEST_ADDOPTS"):
        env.pop(name, None)
    result = subprocess.run(
        pytest_command("-n", "0", "--collect-only", "-q", "tests/teatree_core"),
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert result.returncode == 4, result.stderr
    assert "directory selection records" in result.stderr
    assert "collected" not in result.stdout


@pytest.mark.timeout(120)
def test_real_ci_still_refuses_bare_whole_tree(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "teatree.settings")
    env = os.environ.copy()
    for name in ("CI_NODE_TOTAL", "CI_NODE_INDEX", "PYTEST_ADDOPTS"):
        env.pop(name, None)
    env["CI"] = "true"
    result = subprocess.run(
        pytest_command("-o", "addopts=", "-n", "0", "--collect-only", "-q", "tests"),
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert result.returncode == 4, result.stderr
    assert "unsharded whole-tree pytest is refused" in result.stderr
    assert "collected" not in result.stdout


@pytest.mark.timeout(240)
def test_real_pytest_collects_a_small_directory() -> None:
    env = os.environ.copy()
    for name in ("CI", "CI_NODE_TOTAL", "CI_NODE_INDEX", "PYTEST_ADDOPTS"):
        env.pop(name, None)

    small = subprocess.run(
        pytest_command("-n", "0", "--collect-only", "-q", "tests/teatree_verification"),
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert small.returncode == 0, small.stderr
    assert "collected" in small.stdout


@pytest.mark.timeout(120)
def test_real_tach_scoped_invocation_is_never_refused(tmp_path: Path) -> None:
    """The flags-only shape ``dev/test-affected.sh`` emits on a doc-only diff.

    Reproduces the local lane's flags-only shape with HEAD as an available base.
    A CI checkout need not have origin/main; tach validates the base at startup.
    Collection exits immediately after the startup guard accepts the invocation.
    """
    repo = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    for name in ("CI_NODE_TOTAL", "CI_NODE_INDEX", "PYTEST_ADDOPTS"):
        env.pop(name, None)
    (tmp_path / "stop_after_startup_4998.py").write_text(
        "import pytest\n\n"
        "def pytest_collection(session: pytest.Session) -> None:\n"
        "    pytest.exit('startup accepted', returncode=0)\n",
        encoding="utf-8",
    )
    env["PYTHONPATH"] = os.pathsep.join((str(tmp_path), env.get("PYTHONPATH", "")))
    result = subprocess.run(
        pytest_command(
            "-n",
            "0",
            "--tach",
            "--tach-base",
            "HEAD",
            "-p",
            "teatree.quality.force_keep_plugin",
            "-p",
            "stop_after_startup_4998",
            "--collect-only",
            "-q",
        ),
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "startup accepted" in result.stdout
    assert "unsharded whole-tree pytest is refused" not in result.stderr
