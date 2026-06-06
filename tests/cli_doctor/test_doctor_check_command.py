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
import teatree.paths as teatree_paths
from teatree.cli import app
from teatree.cli.doctor import IntrospectionHelpers

from ._shared import _stage_home, _write_teatree_toml

runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolate_environment_dependent_gates(monkeypatch):
    """Pin the doctor gates that depend on the runner's real on-disk location.

    Two gates read the environment the test runner happens to live in and would
    otherwise make the doctor smoke tests non-deterministic. The clone-currency
    gate (#948) shells out to real ``git fetch`` / ``rev-list`` against whatever
    clone the runner lives in (a lagging checkout surfaces a real FAIL), and the
    entrypoint-is-primary-clone gate (#1507) FAILs when the runner executes from
    a worktree (``paths.DATA_DIR_AUTO_ISOLATED`` is True) — exactly the case
    here. Both are exercised end-to-end in their own dedicated modules
    (``test_clone_guard.py`` and ``test_entrypoint_primary_clone.py``); here we
    only assert that ``t3 doctor check`` aggregates results, so pinning each to
    its primary-clone boundary is correct.
    """
    monkeypatch.setattr(teatree_cli_update, "_collect_repos", list)
    monkeypatch.setattr(teatree_paths, "DATA_DIR_AUTO_ISOLATED", False)


class TestDoctorCheckCommand:
    """End-to-end ``t3 doctor check`` dispatch via ``CliRunner``.

    The command's sanity check runs live against the staged
    ``~/.teatree.toml``; ``editable_info`` + ``shutil.which`` stay mocked
    because they touch the real site-packages and PATH.
    """

    def _write_noop_toml(self, home: Path) -> None:
        _write_teatree_toml(home / ".teatree.toml", "[teatree]\ncontribute = false\n")

    def test_entrypoint_guard_runs_before_editable_autorepair(self, tmp_path, monkeypatch):
        """The entrypoint guard must fire before editable auto-repair (#1507).

        Under ``contribute=true`` the editable-sanity check can auto-make the
        cwd worktree editable — the exact stale anchor the guard catches. If it
        ran first it would create the bad install before the guard fails.
        """
        _stage_home(tmp_path, monkeypatch)
        self._write_noop_toml(tmp_path)

        order: list[str] = []

        def _entry() -> bool:
            order.append("entrypoint")
            return True

        def _editable() -> bool:
            order.append("editable")
            return True

        with (
            patch.object(teatree_cli_doctor.shutil, "which", side_effect=lambda t: f"/usr/bin/{t}"),
            patch.object(teatree_cli_doctor, "_check_entrypoint_is_primary_clone", side_effect=_entry),
            patch.object(teatree_cli_doctor, "_check_editable_sanity", side_effect=_editable),
            patch.object(teatree_overlay_loader, "get_all_overlays", return_value={}),
        ):
            runner.invoke(app, ["doctor", "check"])

        assert order.index("entrypoint") < order.index("editable")

    def test_reports_all_checks_passed(self, tmp_path, monkeypatch):
        _stage_home(tmp_path, monkeypatch)
        self._write_noop_toml(tmp_path)

        with (
            patch.object(teatree_cli_doctor.shutil, "which", side_effect=lambda t: f"/usr/bin/{t}"),
            patch.object(IntrospectionHelpers, "editable_info", return_value=(False, "")),
            patch.object(teatree_overlay_loader, "get_all_overlays", return_value={}),
            patch("teatree.core.gates.schema_guard.pending_migrations", return_value=[]),
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

    def test_configures_django_before_self_db_inspection(self, tmp_path, monkeypatch):
        """``t3 doctor check`` must configure Django before inspecting the self-DB.

        Regression (#126): ``check()`` is a plain Typer command in a
        Django-free group, so without an explicit ``django.setup()`` the
        self-DB schema inspection hit ``ImproperlyConfigured: DJANGO_
        SETTINGS_MODULE not set`` and silently WARNed — masking a real stale
        runtime self-DB that would have locked out the merge path. The check
        must run the canonical ``ensure_django`` step (``django.setup`` +
        ``DJANGO_SETTINGS_MODULE``) before reaching the schema guard, so it
        reports the REAL pending-migration state.
        """
        _stage_home(tmp_path, monkeypatch)
        self._write_noop_toml(tmp_path)

        order: list[str] = []

        def _record_setup() -> None:
            order.append("ensure_django")

        def _record_check(*_args, **_kwargs) -> bool:
            order.append("self_db_check")
            return True

        with (
            patch.object(teatree_cli_doctor.shutil, "which", side_effect=lambda t: f"/usr/bin/{t}"),
            patch.object(IntrospectionHelpers, "editable_info", return_value=(False, "")),
            patch.object(teatree_overlay_loader, "get_all_overlays", return_value={}),
            patch.object(teatree_cli_doctor, "ensure_django", side_effect=_record_setup),
            patch(
                "teatree.core.gates.schema_guard.doctor_check_self_db_migrations",
                side_effect=_record_check,
            ),
        ):
            result = runner.invoke(app, ["doctor", "check"])

        assert result.exit_code == 0, result.output
        # Django must be configured BEFORE the self-DB schema inspection runs.
        assert "ensure_django" in order, "doctor check must call ensure_django (#126)"
        assert order.index("ensure_django") < order.index("self_db_check")
        assert "Could not inspect self-DB migrations: ImproperlyConfigured" not in result.output

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
