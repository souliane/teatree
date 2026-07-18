"""Tests for ``t3 hook run <name>`` — the portable-hook resolver (#3312).

The resolver maps a name against the packaged :mod:`teatree.hooks.portable` set,
runs Python hooks in-process and the shell hook via subprocess, passes the exit
code through unchanged, and refuses an unknown/internal name loudly. These tests
exercise both the library layer (:func:`teatree.hooks.portable.run_hook`) and the
``t3 hook`` CLI surface, using a real ``git`` tree for the shell hook.
"""

import importlib
import subprocess
import sys
from importlib.resources import files
from pathlib import Path

import pytest
from typer.testing import CliRunner

from teatree.cli import app
from teatree.hooks import portable
from teatree.hooks.portable import (
    PORTABLE_HOOKS,
    UnknownHookError,
    available_hook_names,
    check_module_health,
    check_no_silent_skip,
    run_hook,
)

runner = CliRunner()

_PORTABLE_NAMES = {
    "check_module_health",
    "check_no_silent_skip",
    "check_broad_except",
    "check_test_shape",
    "check_test_path_mirror",
    "refuse-main-clone-commit",
}


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],  # noqa: S607 — git is resolved from PATH by design in this integration test helper
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


class TestRegistry:
    def test_available_names_are_the_portable_subset(self) -> None:
        assert set(available_hook_names()) == _PORTABLE_NAMES

    def test_every_python_hook_target_imports_and_exposes_main(self) -> None:
        for hook in PORTABLE_HOOKS.values():
            if hook.kind != "python":
                continue
            module = importlib.import_module(hook.target)
            assert callable(module.main), f"{hook.name} target must expose main()"

    def test_shell_hook_ships_as_an_executable_package_resource(self) -> None:
        resource = files("teatree.hooks.portable").joinpath("refuse-main-clone-commit.sh")
        assert resource.is_file()
        assert resource.read_text(encoding="utf-8").startswith("#!")


class TestRunHookResolution:
    def test_unknown_name_raises_unknown_hook_error(self) -> None:
        # An internal-only script name (a real hook, but NOT portable) is refused.
        with pytest.raises(UnknownHookError):
            run_hook("check_blueprint_sync")

    def test_python_hook_runs_in_process_and_returns_exit_code(self, monkeypatch: pytest.MonkeyPatch) -> None:
        health = check_module_health
        monkeypatch.setattr(health, "_staged_python_files", list)
        assert run_hook("check_module_health") == 0

    def test_python_hook_exit_code_passes_through_unchanged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        skip = check_no_silent_skip
        monkeypatch.setattr(skip, "_staged_test_files", lambda: ["tests/test_x.py"])
        monkeypatch.setattr(
            skip,
            "_staged_source",
            lambda _f: "import pytest\n@pytest.mark.skip\ndef test_a():\n    assert True\n",
        )
        assert run_hook("check_no_silent_skip") == 1

    def test_extra_args_reach_the_python_hook(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: dict[str, str | None] = {}
        health = check_module_health

        def _fake_diff_mode(base_ref: str) -> int:
            seen["base_ref"] = base_ref
            return 0

        monkeypatch.setattr(health, "run_diff_mode", _fake_diff_mode)
        assert run_hook("check_module_health", ["--from-ref", "origin/main"]) == 0
        assert seen["base_ref"] == "origin/main"

    def test_in_process_run_restores_sys_argv(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(check_no_silent_skip, "_staged_test_files", list)
        before = list(sys.argv)
        run_hook("check_no_silent_skip", ["--anything"])
        assert sys.argv == before


class TestShellHook:
    def test_shell_hook_blocks_a_feature_branch_in_the_main_clone(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # A real ``.git`` *directory* (a main clone) on a non-default branch:
        # the worktree-first gate must refuse (exit 1), exercising subprocess
        # resolution of the packaged shell hook and exit-code passthrough.
        _git(tmp_path, "init", "-b", "feature")
        _git(tmp_path, "config", "user.email", "t@example.com")
        _git(tmp_path, "config", "user.name", "t")
        monkeypatch.chdir(tmp_path)
        assert run_hook("refuse-main-clone-commit") == 1

    def test_shell_hook_allows_a_worktree(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        base = tmp_path / "clone"
        base.mkdir()
        _git(base, "init", "-b", "main")
        _git(base, "config", "user.email", "t@example.com")
        _git(base, "config", "user.name", "t")
        (base / "f.txt").write_text("x\n", encoding="utf-8")
        _git(base, "add", "f.txt")
        _git(base, "commit", "-m", "init")
        worktree = tmp_path / "wt"
        _git(base, "worktree", "add", "-b", "feature", str(worktree))
        monkeypatch.chdir(worktree)
        assert run_hook("refuse-main-clone-commit") == 0


class TestHookCli:
    def test_list_shows_every_portable_name(self) -> None:
        result = runner.invoke(app, ["hook", "list"])
        assert result.exit_code == 0
        for name in _PORTABLE_NAMES:
            assert name in result.output

    def test_run_passes_hook_exit_code_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(portable, "run_hook", lambda name, args: 7)
        result = runner.invoke(app, ["hook", "run", "check_module_health"])
        assert result.exit_code == 7

    def test_run_forwards_extra_args(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, object] = {}

        def _spy(name: str, args: list[str]) -> int:
            captured["name"] = name
            captured["args"] = list(args)
            return 0

        monkeypatch.setattr(portable, "run_hook", _spy)
        result = runner.invoke(app, ["hook", "run", "check_module_health", "--from-ref", "origin/main"])
        assert result.exit_code == 0
        assert captured["name"] == "check_module_health"
        assert captured["args"] == ["--from-ref", "origin/main"]

    def test_run_unknown_name_refuses_and_lists_available(self) -> None:
        result = runner.invoke(app, ["hook", "run", "check_cli_reference_sync"])
        assert result.exit_code == 2
        assert "is not a portable hook" in result.output
        for name in _PORTABLE_NAMES:
            assert name in result.output
