"""A teatree test run repairs a project environment from another operating system."""

from subprocess import TimeoutExpired
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from teatree.contrib.t3_teatree.overlay import TeatreeRuntime


def test_empty_project_venv_is_rebuilt_before_tests(tmp_path):
    (tmp_path / ".venv").mkdir()
    worktree = SimpleNamespace(worktree_path=str(tmp_path))

    steps = TeatreeRuntime().pre_run_steps(worktree, "tests")

    assert [step.name for step in steps] == ["sync-dependencies"]


def test_incompatible_project_python_is_rebuilt_before_tests(tmp_path):
    python = tmp_path / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("not a Python executable")
    python.chmod(0o755)
    worktree = SimpleNamespace(worktree_path=str(tmp_path))

    steps = TeatreeRuntime().pre_run_steps(worktree, "tests")

    assert [step.name for step in steps] == ["sync-dependencies"]

    def simulate_command(args, **_kwargs):
        if args[0] == str(python):
            raise OSError

    run_checked = Mock(side_effect=simulate_command)
    with patch.dict(steps[0].callable.__globals__, {"run_checked": run_checked}):
        steps[0].callable()
    assert not (tmp_path / ".venv").exists()
    assert run_checked.call_count == 2
    assert run_checked.call_args_list[-1] == call(["uv", "sync"], cwd=tmp_path)


def test_a_probe_timeout_reaches_the_environment_repair_step(tmp_path):
    python = tmp_path / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("stalled interpreter")
    worktree = SimpleNamespace(worktree_path=str(tmp_path))

    def simulate_command(args, **_kwargs):
        if args[0] == str(python):
            raise TimeoutExpired(args, 5)

    run_checked = Mock(side_effect=simulate_command)
    with patch.dict(TeatreeRuntime.pre_run_steps.__globals__, {"run_checked": run_checked}):
        steps = TeatreeRuntime().pre_run_steps(worktree, "tests")

    assert [step.name for step in steps] == ["sync-dependencies"]
    with patch.dict(steps[0].callable.__globals__, {"run_checked": run_checked}):
        steps[0].callable()

    assert not (tmp_path / ".venv").exists()
    assert run_checked.call_args_list[-1] == call(["uv", "sync"], cwd=tmp_path)


def test_unusable_venv_symlink_is_removed_without_touching_its_target(tmp_path):
    target = tmp_path / "shared-environment"
    target.mkdir()
    sentinel = target / "keep"
    sentinel.write_text("shared")
    (tmp_path / ".venv").symlink_to(target, target_is_directory=True)
    worktree = SimpleNamespace(worktree_path=str(tmp_path))

    steps = TeatreeRuntime().pre_run_steps(worktree, "tests")

    assert [step.name for step in steps] == ["sync-dependencies"]

    def simulate_command(args, **_kwargs):
        if args[0] == str(tmp_path / ".venv" / "bin" / "python"):
            raise OSError

    run_checked = Mock(side_effect=simulate_command)
    with patch.dict(steps[0].callable.__globals__, {"run_checked": run_checked}):
        steps[0].callable()

    assert not (tmp_path / ".venv").is_symlink()
    assert sentinel.read_text() == "shared"
    assert run_checked.call_args_list[-1] == call(["uv", "sync"], cwd=tmp_path)
