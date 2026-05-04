"""Tests for the module-health pre-commit hook."""

from pathlib import Path

import pytest

import scripts.hooks.check_module_health as mod
from scripts.hooks.commit_message import commit_message_has_relax_prefix


class TestRelaxBypass:
    def test_blocks_when_dict_object_added_without_relax(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """A new dict[str, object] annotation blocks the commit by default."""
        target = tmp_path / "src" / "foo.py"
        target.parent.mkdir(parents=True)
        target.write_text("def f() -> dict[str, object]:\n    return {}\n", encoding="utf-8")

        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(mod, "_staged_python_files", lambda: ["src/foo.py"])
        monkeypatch.setattr(mod, "_added_line_numbers", lambda _f: {1})
        monkeypatch.setattr("sys.argv", ["check_module_health.py"])

        assert mod.main() == 1

    def test_relax_prefix_bypasses_dict_object_violation(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """When the commit message starts with ``relax:`` the hook accepts the diff."""
        target = tmp_path / "src" / "foo.py"
        target.parent.mkdir(parents=True)
        target.write_text("def f() -> dict[str, object]:\n    return {}\n", encoding="utf-8")

        msg_file = tmp_path / "COMMIT_EDITMSG"
        msg_file.write_text("relax(scope): add dict[str, object] for legacy api\n", encoding="utf-8")

        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(mod, "_staged_python_files", lambda: ["src/foo.py"])
        monkeypatch.setattr(mod, "_added_line_numbers", lambda _f: {1})
        monkeypatch.setattr("sys.argv", ["check_module_health.py", str(msg_file)])

        assert mod.main() == 0


class TestSharedCommitMessageHelper:
    def test_relax_prefix_match(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        msg_file = tmp_path / "COMMIT_EDITMSG"
        msg_file.write_text("relax: drop coverage threshold for vendored module\n", encoding="utf-8")
        monkeypatch.setattr("sys.argv", ["hook.py", str(msg_file)])
        assert commit_message_has_relax_prefix() is True

    def test_relax_with_scope(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        msg_file = tmp_path / "COMMIT_EDITMSG"
        msg_file.write_text("relax(api): add dict[str, object] for compat\n", encoding="utf-8")
        monkeypatch.setattr("sys.argv", ["hook.py", str(msg_file)])
        assert commit_message_has_relax_prefix() is True

    def test_non_relax_subject(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        msg_file = tmp_path / "COMMIT_EDITMSG"
        msg_file.write_text("feat: add a thing\n", encoding="utf-8")
        monkeypatch.setattr("sys.argv", ["hook.py", str(msg_file)])
        assert commit_message_has_relax_prefix() is False

    def test_returns_false_when_no_message_file(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.argv", ["hook.py"])
        # No commit-msg path argument and no .git dir resolvable; the hook
        # should treat absence as "no relax" so it blocks rather than bypassing.
        import scripts.hooks.commit_message as helper  # noqa: PLC0415

        monkeypatch.setattr(helper, "_find_git_dir", lambda: None)
        assert commit_message_has_relax_prefix() is False
