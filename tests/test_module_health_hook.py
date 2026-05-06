"""Tests for the module-health pre-commit hook."""

from pathlib import Path

import pytest

import scripts.hooks.check_module_health as mod


class TestModuleHealthGate:
    def test_blocks_when_dict_object_added(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """A new dict[str, object] annotation blocks the commit."""
        target = tmp_path / "src" / "foo.py"
        target.parent.mkdir(parents=True)
        target.write_text("def f() -> dict[str, object]:\n    return {}\n", encoding="utf-8")

        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(mod, "_staged_python_files", lambda: ["src/foo.py"])
        monkeypatch.setattr(mod, "_added_line_numbers", lambda _f: {1})
        monkeypatch.setattr("sys.argv", ["check_module_health.py"])

        assert mod.main() == 1

    def test_relax_commit_message_does_not_bypass(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """A ``relax:`` commit subject must NOT bypass the gate (regression for #525)."""
        target = tmp_path / "src" / "foo.py"
        target.parent.mkdir(parents=True)
        target.write_text("def f() -> dict[str, object]:\n    return {}\n", encoding="utf-8")

        msg_file = tmp_path / "COMMIT_EDITMSG"
        msg_file.write_text("relax(scope): add dict[str, object] for legacy api\n", encoding="utf-8")

        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(mod, "_staged_python_files", lambda: ["src/foo.py"])
        monkeypatch.setattr(mod, "_added_line_numbers", lambda _f: {1})
        monkeypatch.setattr("sys.argv", ["check_module_health.py", str(msg_file)])

        assert mod.main() == 1
