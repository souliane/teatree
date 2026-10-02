"""Integration tests for the ``check_pr_body_stray`` portable hook (#3581).

Exercises the hook against a real ``git`` tree: a hand-named ``pr-body.*`` staged
inside the worktree is refused (exit 1), a clean stage passes (exit 0). Run via
``t3 hook run check_pr_body_stray`` in any repo.
"""

import subprocess
from pathlib import Path

import pytest

from teatree.hooks.portable import run_hook
from teatree.hooks.portable.check_pr_body_stray import main


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],  # noqa: S607 — git resolved from PATH by design in this integration helper
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def _init_repo(root: Path) -> None:
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")


class TestStagedRejection:
    def test_staged_pr_body_file_is_refused(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        (tmp_path / "pr-body.md").write_text("type(scope): summary\n", encoding="utf-8")
        _git(tmp_path, "add", "pr-body.md")
        monkeypatch.chdir(tmp_path)
        assert main() == 1

    def test_underscore_variant_is_refused(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        (tmp_path / "pr_body.md").write_text("x\n", encoding="utf-8")
        _git(tmp_path, "add", "pr_body.md")
        monkeypatch.chdir(tmp_path)
        assert main() == 1

    def test_clean_stage_passes(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
        _git(tmp_path, "add", "app.py")
        monkeypatch.chdir(tmp_path)
        assert main() == 0

    def test_empty_stage_passes(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        monkeypatch.chdir(tmp_path)
        assert main() == 0

    def test_resolves_through_the_portable_registry(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        (tmp_path / "pr-body.md").write_text("x\n", encoding="utf-8")
        _git(tmp_path, "add", "pr-body.md")
        monkeypatch.chdir(tmp_path)
        assert run_hook("check_pr_body_stray") == 1
