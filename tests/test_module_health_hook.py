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
        monkeypatch.setattr(mod, "_added_line_numbers", lambda _f, _h: {1})
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
        monkeypatch.setattr(mod, "_added_line_numbers", lambda _f, _h: {1})
        monkeypatch.setattr("sys.argv", ["check_module_health.py", str(msg_file)])

        assert mod.main() == 1


class TestWholeTreeDebtReport:
    """souliane/teatree#3511 — the over-cap set is visible before it blocks."""

    def _tree(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        (src / "over.py").write_text("x = 1\n" * (mod.MAX_LOC + 3), encoding="utf-8")
        (src / "under.py").write_text("x = 1\n", encoding="utf-8")
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_over.py").write_text("x = 1\n" * (mod.MAX_LOC + 3), encoding="utf-8")

    def test_report_lists_over_cap_first_party_modules(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self._tree(tmp_path)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr("sys.argv", ["check_module_health.py", "--report-debt"])

        assert mod.main() == 0
        out = capsys.readouterr().out
        assert "src/over.py" in out
        assert "src/under.py" not in out
        assert "tests/test_over.py" not in out

    def test_report_is_advisory_and_never_blocks(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self._tree(tmp_path)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr("sys.argv", ["check_module_health.py", "--report-debt"])

        assert mod.main() == 0
        assert "advisory" in capsys.readouterr().out

    def test_report_says_clean_when_nothing_is_over_cap(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "small.py").write_text("x = 1\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr("sys.argv", ["check_module_health.py", "--report-debt"])

        assert mod.main() == 0
        assert "no first-party module is over" in capsys.readouterr().out

    def test_report_lists_over_cap_module_function_counts(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (tmp_path / "src").mkdir()
        body = "".join(f"def f{i}() -> None:\n    pass\n" for i in range(mod.MAX_MODULE_FUNCTIONS + 2))
        (tmp_path / "src" / "many.py").write_text(body, encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr("sys.argv", ["check_module_health.py", "--report-debt"])

        assert mod.main() == 0
        out = capsys.readouterr().out
        assert "src/many.py" in out
        assert "public module-level functions" in out
