"""``t3 doctor check`` — end-to-end CLI dispatch via ``CliRunner``.

Lifted verbatim from the former monolithic ``tests/test_cli_doctor.py``
(souliane/teatree#443). No behavior change: same assertions and helpers,
only relocated under a focused package by concern. The module-level
``runner = CliRunner()`` from the old module is instantiated here since
this is now its only consumer.
"""

from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

import teatree.cli.doctor as teatree_cli_doctor
import teatree.cli.update as teatree_cli_update
import teatree.core.overlay_loader as teatree_overlay_loader
from teatree.cli import app
from teatree.cli.doctor import IntrospectionHelpers

from ._shared import _stage_home, _write_teatree_toml

runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolate_clone_currency(monkeypatch):
    """Pin the pre-investigation clone-currency gate (#948) to "no repos".

    The gate calls ``_collect_repos()`` → real ``git fetch origin`` and
    ``rev-list HEAD..origin/<default>`` against whatever clone the test
    runner happens to live in.  In a CI checkout that lags behind
    ``origin/main`` (e.g. a feature branch cut weeks ago) this surfaces
    a real ``FAIL`` line and makes the doctor smoke tests non-deterministic.
    The doctor wiring itself is exercised end-to-end in
    ``tests/teatree_core/test_clone_guard.py``; here we only assert that
    ``t3 doctor check`` aggregates check results, so an empty repo list
    is the right boundary.
    """
    monkeypatch.setattr(teatree_cli_update, "_collect_repos", list)


class TestDoctorCheckCommand:
    """End-to-end ``t3 doctor check`` dispatch via ``CliRunner``.

    The command's sanity check runs live against the staged
    ``~/.teatree.toml``; ``editable_info`` + ``shutil.which`` stay mocked
    because they touch the real site-packages and PATH.
    """

    def _write_noop_toml(self, home: Path) -> None:
        _write_teatree_toml(home / ".teatree.toml", "[teatree]\ncontribute = false\n")

    def test_reports_all_checks_passed(self, tmp_path, monkeypatch):
        _stage_home(tmp_path, monkeypatch)
        self._write_noop_toml(tmp_path)

        with (
            patch.object(teatree_cli_doctor.shutil, "which", side_effect=lambda t: f"/usr/bin/{t}"),
            patch.object(IntrospectionHelpers, "editable_info", return_value=(False, "")),
            patch.object(teatree_overlay_loader, "get_all_overlays", return_value={}),
        ):
            result = runner.invoke(app, ["doctor", "check"])

        assert result.exit_code == 0
        assert "All checks passed" in result.output

    def test_reports_warning_when_editable_state_mismatches(self, tmp_path, monkeypatch):
        _stage_home(tmp_path, monkeypatch)
        # contribute=false but teatree is editable → WARN
        self._write_noop_toml(tmp_path)

        with (
            patch.object(teatree_cli_doctor.shutil, "which", side_effect=lambda t: f"/usr/bin/{t}"),
            patch.object(IntrospectionHelpers, "editable_info", return_value=(True, "file:///src")),
            patch.object(teatree_overlay_loader, "get_all_overlays", return_value={}),
        ):
            result = runner.invoke(app, ["doctor", "check"])

        assert result.exit_code == 0
        assert "WARN" in result.output

    def test_fails_when_required_tool_missing(self, tmp_path, monkeypatch):
        _stage_home(tmp_path, monkeypatch)
        self._write_noop_toml(tmp_path)

        with (
            patch.object(
                teatree_cli_doctor.shutil,
                "which",
                side_effect=lambda t: None if t == "direnv" else f"/usr/bin/{t}",
            ),
            patch.object(IntrospectionHelpers, "editable_info", return_value=(False, "")),
            patch.object(teatree_overlay_loader, "get_all_overlays", return_value={}),
        ):
            result = runner.invoke(app, ["doctor", "check"])

        assert "FAIL  Required tool not found: direnv" in result.output

    def test_validates_skills_in_claude_dir(self, tmp_path, monkeypatch):
        _stage_home(tmp_path, monkeypatch)
        self._write_noop_toml(tmp_path)
        claude_skills = tmp_path / ".claude" / "skills"
        (claude_skills / "ok-skill").mkdir(parents=True)
        (claude_skills / "ok-skill" / "SKILL.md").write_text("---\nname: ok-skill\ndescription: d\n---\n")

        with (
            patch.object(teatree_cli_doctor.shutil, "which", side_effect=lambda t: f"/usr/bin/{t}"),
            patch.object(IntrospectionHelpers, "editable_info", return_value=(False, "")),
            patch.object(teatree_overlay_loader, "get_all_overlays", return_value={}),
        ):
            result = runner.invoke(app, ["doctor", "check"])

        assert result.exit_code == 0
        assert "1 skill(s) validated" in result.output

    def test_reports_skill_validation_errors(self, tmp_path, monkeypatch):
        _stage_home(tmp_path, monkeypatch)
        self._write_noop_toml(tmp_path)
        bad = tmp_path / ".claude" / "skills" / "bad-skill"
        bad.mkdir(parents=True)
        (bad / "SKILL.md").write_text("no frontmatter here")

        with (
            patch.object(teatree_cli_doctor.shutil, "which", side_effect=lambda t: f"/usr/bin/{t}"),
            patch.object(IntrospectionHelpers, "editable_info", return_value=(False, "")),
            patch.object(teatree_overlay_loader, "get_all_overlays", return_value={}),
        ):
            result = runner.invoke(app, ["doctor", "check"])

        assert "FAIL" in result.output

    def test_reports_skill_validation_warnings(self, tmp_path, monkeypatch):
        _stage_home(tmp_path, monkeypatch)
        self._write_noop_toml(tmp_path)
        skill = tmp_path / ".claude" / "skills" / "warn-skill"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("---\nname: warn-skill\ndescription: d\nunknown-field: x\n---\n")

        with (
            patch.object(teatree_cli_doctor.shutil, "which", side_effect=lambda t: f"/usr/bin/{t}"),
            patch.object(IntrospectionHelpers, "editable_info", return_value=(False, "")),
            patch.object(teatree_overlay_loader, "get_all_overlays", return_value={}),
        ):
            result = runner.invoke(app, ["doctor", "check"])

        assert "WARN" in result.output

    def test_fails_on_import_error(self):
        import builtins  # noqa: PLC0415

        real_import = builtins.__import__

        def fail_import(name, *args, **kwargs):
            if name == "teatree.core":
                raise ImportError(name)
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=fail_import):
            result = runner.invoke(app, ["doctor", "check"])

        assert "FAIL" in result.output
