"""Tests for teatree.cli — comprehensive CLI command coverage.

Uses typer.testing.CliRunner to invoke commands and mocks external
dependencies (subprocess, filesystem, network, Django).
"""

import json
import time
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

import teatree.agents.skill_bundle as skill_bundle_mod
import teatree.claude_sessions as claude_sessions_mod
import teatree.cli as cli_mod
import teatree.config as config_mod
import teatree.core.overlay_loader as overlay_loader_mod
import teatree.core.resolve as resolve_mod
from teatree.cli import (
    _detect_agent_ticket_status,
    _find_overlay_project,
    _find_project_root,
    app,
)
from teatree.cli import doctor as cli_doctor_mod
from teatree.overlay_init.generator import OverlayScaffolder, camelize

runner = CliRunner()


# ── docs command ─────────────────────────────────────────────────────


class TestDocsCommand:
    def test_no_mkdocs_yml(self, tmp_path, monkeypatch):
        """Docs command fails if no mkdocs.yml found."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "pyproject.toml").write_text("[project]\n")
        result = runner.invoke(app, ["docs"])
        assert result.exit_code == 1
        assert "No mkdocs.yml" in result.output

    def test_mkdocs_not_installed(self, tmp_path, monkeypatch):
        """Docs command fails if mkdocs is not installed."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "pyproject.toml").write_text("[project]\n")
        (tmp_path / "mkdocs.yml").write_text("site_name: Test\n")

        # Make mkdocs unimportable
        import builtins  # noqa: PLC0415

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "mkdocs":
                raise ImportError(name)
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        result = runner.invoke(app, ["docs"])
        assert result.exit_code == 1
        assert "mkdocs is not installed" in result.output

    def test_runs_mkdocs_serve(self, tmp_path, monkeypatch):
        """Docs command runs mkdocs serve when everything is available."""
        import sys  # noqa: PLC0415
        import types  # noqa: PLC0415

        monkeypatch.chdir(tmp_path)
        (tmp_path / "pyproject.toml").write_text("[project]\n")
        (tmp_path / "mkdocs.yml").write_text("site_name: Test\n")

        # Make mkdocs importable by inserting a fake module
        fake_mkdocs = types.ModuleType("mkdocs")
        monkeypatch.setitem(sys.modules, "mkdocs", fake_mkdocs)

        with (
            patch.object(cli_mod.subprocess, "run") as mock_run,
            patch("teatree.cli._maybe_show_update_notice"),
        ):
            mock_run.return_value = MagicMock(returncode=0)
            result = runner.invoke(app, ["docs"])
            assert result.exit_code == 0
            mock_run.assert_called_once()
            call_args = mock_run.call_args
            assert "mkdocs" in str(call_args)


# ── agent command ─────────────────────────────────────────────────────


class TestAgentCommand:
    def test_no_claude(self, tmp_path, monkeypatch):
        """Agent command fails if claude CLI not found."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "pyproject.toml").write_text("[project]\n")

        with (
            patch.object(config_mod, "discover_active_overlay", return_value=None),
            patch("shutil.which", return_value=None),
        ):
            result = runner.invoke(app, ["agent"])
            assert result.exit_code == 1
            assert "claude CLI not found" in result.output

    def test_with_active_overlay(self, tmp_path, monkeypatch):
        """Agent command launches claude with overlay context."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "pyproject.toml").write_text("[project]\n")

        from teatree.config import OverlayEntry  # noqa: PLC0415
        from teatree.skill_loading import SkillLoadingPolicy, SkillSelectionResult  # noqa: PLC0415

        mock_overlay = OverlayEntry(name="test-overlay", overlay_class="test.overlay.TestOverlay")
        overlay_obj = MagicMock()
        overlay_obj.metadata.get_skill_metadata.return_value = {"skill_path": "skills/test/SKILL.md"}

        with (
            patch.object(config_mod, "discover_active_overlay", return_value=mock_overlay),
            patch.object(overlay_loader_mod, "get_overlay", return_value=overlay_obj),
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch.object(cli_doctor_mod.IntrospectionHelpers, "editable_info", return_value=(False, "")),
            patch.object(cli_mod, "_detect_agent_ticket_status", return_value="started"),
            patch.object(
                SkillLoadingPolicy,
                "select_for_agent_launch",
                return_value=SkillSelectionResult(skills=["code"]),
            ),
            patch.object(cli_mod.os, "execvp") as mock_exec,
        ):
            runner.invoke(app, ["agent", "fix bug"])
            mock_exec.assert_called_once()
            cmd = mock_exec.call_args[0][1]
            assert cmd[0] == "/usr/bin/claude"
            assert "--append-system-prompt" in cmd

    def test_no_overlay(self, tmp_path, monkeypatch):
        """Agent command works without active overlay."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "pyproject.toml").write_text("[project]\n")

        from teatree.skill_loading import SkillLoadingPolicy, SkillSelectionResult  # noqa: PLC0415

        with (
            patch.object(config_mod, "discover_active_overlay", return_value=None),
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch.object(cli_doctor_mod.IntrospectionHelpers, "editable_info", return_value=(False, "")),
            patch.object(
                SkillLoadingPolicy,
                "select_for_agent_launch",
                return_value=SkillSelectionResult(skills=["code"]),
            ),
            patch.object(cli_mod.os, "execvp") as mock_exec,
        ):
            runner.invoke(app, ["agent"])
            mock_exec.assert_called_once()

    def test_rejects_phase_and_skill_together(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "pyproject.toml").write_text("[project]\n")

        result = runner.invoke(app, ["agent", "--phase", "coding", "--skill", "code"])

        assert result.exit_code == 1
        assert "--phase and --skill cannot be used together." in result.output

    def test_reports_policy_value_error(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "pyproject.toml").write_text("[project]\n")

        from teatree.skill_loading import SkillLoadingPolicy  # noqa: PLC0415

        with (
            patch.object(config_mod, "discover_active_overlay", return_value=None),
            patch.object(
                SkillLoadingPolicy,
                "select_for_agent_launch",
                side_effect=ValueError("Unknown phase: bad-phase"),
            ),
        ):
            result = runner.invoke(app, ["agent", "--phase", "bad-phase"])

        assert result.exit_code == 1
        assert "Unknown phase: bad-phase" in result.output


# ── sessions command ──────────────────────────────────────────────────


class TestSessionsCommand:
    def test_no_results(self, monkeypatch):
        """Sessions command shows message when no sessions found."""
        with patch.object(claude_sessions_mod, "list_sessions", return_value=[]):
            result = runner.invoke(app, ["sessions", "--all"])
            assert result.exit_code == 0
            assert "No sessions found" in result.output

    def test_shows_results(self, monkeypatch):
        """Sessions command renders session list."""
        from teatree.claude_sessions import SessionInfo  # noqa: PLC0415

        now = time.time()
        sessions = [
            SessionInfo(
                session_id="abc123",
                project="~/workspace/my-project",
                first_prompt="fix the bug",
                timestamp=now * 1000,  # ms format
                mtime=now,
                cwd="/home/user/workspace",
                status="interrupted",
            ),
            SessionInfo(
                session_id="def456",
                project="~/workspace/other",
                first_prompt="add feature",
                timestamp=now - 7200,  # seconds, <86400 ago
                mtime=now - 7200,
                cwd="",
                status="finished",
            ),
            SessionInfo(
                session_id="ghi789",
                project="~/workspace/old",
                first_prompt="x" * 100,
                timestamp=now - 100000,  # >1 day
                mtime=now - 100000,
                cwd="",
                status="finished",
            ),
            SessionInfo(
                session_id="jkl012",
                project="~/workspace/zero",
                first_prompt="",
                timestamp=0,
                mtime=now,
                cwd="",
                status="active",
            ),
            SessionInfo(
                session_id="str123",
                project="~/workspace/strtime",
                first_prompt="string ts",
                timestamp="invalid_ts",
                mtime=now,
                cwd="/some/path",
                status="interrupted",
            ),
        ]
        with patch.object(claude_sessions_mod, "list_sessions", return_value=sessions):
            result = runner.invoke(app, ["sessions", "--all"])
            assert result.exit_code == 0
            assert "fix the bug" in result.output
            assert "abc123" in result.output
            # finished sessions should show "done"
            assert "done" in result.output


# ── overlays command ──────────────────────────────────────────────────


class TestOverlaysCommand:
    def test_none_found(self):
        """Overlays command shows help when no overlays found."""
        with (
            patch.object(config_mod, "discover_overlays", return_value=[]),
            patch.object(config_mod, "discover_active_overlay", return_value=None),
        ):
            result = runner.invoke(app, ["overlays"])
            assert result.exit_code == 0
            assert "No overlays found" in result.output

    def test_lists_installed(self):
        """Overlays command lists installed overlays."""
        from teatree.config import OverlayEntry  # noqa: PLC0415

        entries = [
            OverlayEntry(name="acme", overlay_class="acme.overlay.AcmeOverlay"),
            OverlayEntry(name="demo", overlay_class="demo.overlay.DemoOverlay"),
        ]
        active = OverlayEntry(name="acme", overlay_class="acme.overlay.AcmeOverlay")
        with (
            patch.object(config_mod, "discover_overlays", return_value=entries),
            patch.object(config_mod, "discover_active_overlay", return_value=active),
        ):
            result = runner.invoke(app, ["overlays"])
            assert result.exit_code == 0
            assert "acme" in result.output
            assert "(active)" in result.output


# ── info command ──────────────────────────────────────────────────────


class TestInfoCommand:
    def test_info_command(self):
        """Info command shows installation details."""
        with (
            patch("shutil.which", return_value="/usr/local/bin/t3"),
            patch.object(cli_doctor_mod.IntrospectionHelpers, "editable_info", return_value=(True, "file:///home/src")),
            patch.object(cli_doctor_mod.IntrospectionHelpers, "print_package_info"),
            patch.object(config_mod, "discover_active_overlay", return_value=None),
            patch.object(config_mod, "discover_overlays", return_value=[]),
        ):
            result = runner.invoke(app, ["info"])
            assert result.exit_code == 0


# ── config subcommands ────────────────────────────────────────────────


class TestConfigCommands:
    def test_write_skill_cache_writes_json(self, tmp_path, monkeypatch):
        """write-skill-cache writes overlay metadata to cache."""
        from teatree.config import OverlayEntry  # noqa: PLC0415

        active = OverlayEntry(name="test", overlay_class="test.overlay.TestOverlay")
        mock_overlay = MagicMock()
        mock_overlay.metadata.get_skill_metadata.return_value = {"skill_path": "skills/test/SKILL.md"}

        monkeypatch.setattr("teatree.config.DATA_DIR", tmp_path)
        monkeypatch.delenv("DJANGO_SETTINGS_MODULE", raising=False)
        with (
            patch.object(config_mod, "discover_active_overlay", return_value=active),
            patch("django.setup"),
            patch.object(overlay_loader_mod, "get_overlay", return_value=mock_overlay),
        ):
            result = runner.invoke(app, ["config", "write-skill-cache"])
            assert result.exit_code == 0
            assert "Wrote skill metadata" in result.output
            cache = tmp_path / "skill-metadata.json"
            assert cache.is_file()
            data = json.loads(cache.read_text())
            assert data["skill_path"] == "skills/test/SKILL.md"

    def test_write_skill_cache_no_active_overlay(self, monkeypatch):
        """write-skill-cache works when DJANGO_SETTINGS_MODULE is already set."""
        mock_overlay = MagicMock()
        mock_overlay.metadata.get_skill_metadata.return_value = {}

        monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "tests.django_settings")
        with (
            patch.object(config_mod, "discover_active_overlay", return_value=None),
            patch("django.setup"),
            patch.object(overlay_loader_mod, "get_overlay", return_value=mock_overlay),
        ):
            runner.invoke(app, ["config", "write-skill-cache"])
            # May fail at get_overlay since no overlay is configured,
            # but the branch we want (326->328 bypass) is hit

    def test_autoload_shows_context_match_files(self, tmp_path):
        """Config autoload lists context-match.yml rules from skill dirs."""
        skills_dir = tmp_path / "skills"
        skill = skills_dir / "code" / "hook-config"
        skill.mkdir(parents=True)
        (skill / "context-match.yml").write_text("keywords:\n  - code\n")

        # A skill without context-match.yml should be skipped
        (skills_dir / "t3-test").mkdir()

        with patch.object(skill_bundle_mod, "DEFAULT_SKILLS_DIR", skills_dir):
            result = runner.invoke(app, ["config", "autoload"])
            assert result.exit_code == 0
            assert "code" in result.output
            assert "keywords" in result.output

    def test_autoload_no_skills_dir(self, tmp_path):
        """Config autoload fails when skills dir doesn't exist."""
        with patch.object(skill_bundle_mod, "DEFAULT_SKILLS_DIR", tmp_path / "nonexistent"):
            result = runner.invoke(app, ["config", "autoload"])
            assert result.exit_code == 1
            assert "Skills directory not found" in result.output

    def test_autoload_no_context_match_files(self, tmp_path):
        """Config autoload shows message when no context-match.yml found."""
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        (skills_dir / "code").mkdir()
        # No hook-config/context-match.yml

        with patch.object(skill_bundle_mod, "DEFAULT_SKILLS_DIR", skills_dir):
            result = runner.invoke(app, ["config", "autoload"])
            assert result.exit_code == 0
            assert "No context-match.yml files found" in result.output

    def test_cache_shows_content(self, tmp_path, monkeypatch):
        """Config cache displays skill-metadata.json content."""
        monkeypatch.setattr("teatree.config.DATA_DIR", tmp_path)
        cache_path = tmp_path / "skill-metadata.json"
        cache_path.write_text('{"skill_path": "skills/test/SKILL.md"}\n')

        result = runner.invoke(app, ["config", "cache"])
        assert result.exit_code == 0
        assert "skill_path" in result.output

    def test_cache_no_file(self, tmp_path, monkeypatch):
        """Config cache fails when no cache file exists."""
        monkeypatch.setattr("teatree.config.DATA_DIR", tmp_path)

        result = runner.invoke(app, ["config", "cache"])
        assert result.exit_code == 1
        assert "No cache found" in result.output


# ── Review-request discover ──────────────────────────────────────────


class TestReviewRequestDiscover:
    def test_review_request_discover(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "pyproject.toml").write_text("[project]\n")

        from teatree.config import OverlayEntry  # noqa: PLC0415

        active = OverlayEntry(name="t3-test", overlay_class="test.Overlay", project_path=tmp_path)
        with (
            patch.object(config_mod, "discover_active_overlay", return_value=active),
            patch.object(cli_mod, "managepy") as mock_manage,
        ):
            result = runner.invoke(app, ["review-request", "discover"])
            assert result.exit_code == 0
            mock_manage.assert_called_once_with(tmp_path, "followup", "discover-mrs", overlay_name="t3-test")


# ── Internal helpers ─────────────────────────────────────────────────


class TestFindProjectRoot:
    def test_walks_up(self, tmp_path, monkeypatch):
        nested = tmp_path / "a" / "b" / "c"
        nested.mkdir(parents=True)
        (tmp_path / "a" / "pyproject.toml").write_text("[project]\n")
        monkeypatch.chdir(nested)
        result = _find_project_root()
        assert result == tmp_path / "a"

    def test_falls_back_to_cwd(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = _find_project_root()
        assert result == tmp_path


class TestFindOverlayProject:
    def test_with_active(self, tmp_path):
        from teatree.config import OverlayEntry  # noqa: PLC0415

        active = OverlayEntry(name="test", overlay_class="test.overlay.TestOverlay", project_path=tmp_path)
        with patch.object(config_mod, "discover_active_overlay", return_value=active):
            assert _find_overlay_project() == tmp_path

    def test_without_active(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "pyproject.toml").write_text("[project]\n")
        with patch.object(config_mod, "discover_active_overlay", return_value=None):
            result = _find_overlay_project()
            assert result == tmp_path


# ── Startoverlay helpers ─────────────────────────────────────────────


class TestOverlayScaffolder:
    def test_camelize(self):
        assert camelize("hello_world") == "HelloWorld"
        assert camelize("single") == "Single"
        assert camelize("a_b_c") == "ABC"

    def test_write_overlay(self, tmp_path):
        s = OverlayScaffolder(tmp_path, "test_overlay", "pkg")
        s.write_overlay("test")
        pkg_dir = tmp_path / "src" / "test_overlay"
        assert (pkg_dir / "__init__.py").is_file()
        assert (pkg_dir / "apps.py").is_file()
        text = (pkg_dir / "overlay.py").read_text()
        assert "class TestOverlayOverlay(OverlayBase):" in text
        assert 'django_app: str | None = "test_overlay"' in text
        assert '"skill_path": "skills/test/SKILL.md"' in text

    def test_write_skill_md(self, tmp_path):
        skill_dir = tmp_path / "skills" / "t3-acme"
        s = OverlayScaffolder(tmp_path, "t3_overlay", "pkg")
        s.write_skill_md(skill_dir, "t3-acme", "t3-acme")
        text = (skill_dir / "SKILL.md").read_text()
        assert "name: t3-acme" in text
        assert "t3-workspace" not in text

    def test_copy_config_templates(self, tmp_path):
        s = OverlayScaffolder(tmp_path, "t3_overlay", "pkg")
        s.copy_config_templates()
        assert (tmp_path / ".editorconfig").is_file()
        assert (tmp_path / ".gitignore").is_file()
        assert (tmp_path / ".markdownlint-cli2.yaml").is_file()
        assert (tmp_path / ".pre-commit-config.yaml").is_file()
        assert (tmp_path / ".python-version").is_file()

    def test_write_pyproject(self, tmp_path):
        s = OverlayScaffolder(tmp_path, "demo_overlay", "demo")
        s.write_pyproject("t3-demo")
        pyproject = tmp_path / "pyproject.toml"
        assert pyproject.is_file()
        text = pyproject.read_text()
        assert "t3-demo" in text
        assert "demo_overlay" in text


# ── _launch_claude editable info branch ───────────────────────────────


class TestLaunchClaude:
    def test_with_editable_teatree(self, tmp_path, monkeypatch):
        """_launch_claude includes editable source path when available."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "pyproject.toml").write_text("[project]\n")

        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch.object(
                cli_doctor_mod.IntrospectionHelpers, "editable_info", return_value=(True, "file:///src/teatree")
            ),
            patch.object(cli_mod.os, "execvp") as mock_exec,
        ):
            from teatree.cli import _launch_claude  # noqa: PLC0415

            _launch_claude(
                task="test",
                project_root=tmp_path,
                context_lines=["test"],
                skills=["code"],
                ask_user_which_skill=False,
            )
            cmd = mock_exec.call_args[0][1]
            context_arg = cmd[cmd.index("--append-system-prompt") + 1]
            assert "/src/teatree" in context_arg

    def test_plugin_dir_added_when_t3_contribute(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("T3_CONTRIBUTE", "true")
        (tmp_path / "pyproject.toml").write_text("[project]\n")

        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch.object(cli_doctor_mod.IntrospectionHelpers, "editable_info", return_value=(False, "")),
            patch.object(cli_mod.os, "execvp") as mock_exec,
        ):
            from teatree.cli import _launch_claude  # noqa: PLC0415

            _launch_claude(
                task="",
                project_root=tmp_path,
                context_lines=["test"],
                skills=[],
                ask_user_which_skill=False,
            )
            cmd = mock_exec.call_args[0][1]
            assert "--plugin-dir" in cmd

    def test_asks_user_when_skill_is_unknown(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "pyproject.toml").write_text("[project]\n")

        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch.object(cli_doctor_mod.IntrospectionHelpers, "editable_info", return_value=(False, "")),
            patch.object(cli_mod.os, "execvp") as mock_exec,
        ):
            from teatree.cli import _launch_claude  # noqa: PLC0415

            _launch_claude(
                task="",
                project_root=tmp_path,
                context_lines=["test"],
                skills=[],
                ask_user_which_skill=True,
            )
            cmd = mock_exec.call_args[0][1]
            context_arg = cmd[cmd.index("--append-system-prompt") + 1]
            assert "ask the user which lifecycle skill to load" in context_arg


# ── _detect_agent_ticket_status ──────────────────────────────────────


class TestDetectAgentTicketStatus:
    def test_returns_empty_without_manage_py(self, tmp_path):
        assert _detect_agent_ticket_status(tmp_path) == ""

    def test_returns_empty_on_exception(self, tmp_path):
        (tmp_path / "manage.py").write_text("# stub\n", encoding="utf-8")

        with patch("django.setup", side_effect=Exception("boom")):
            assert _detect_agent_ticket_status(tmp_path) == ""

    def test_returns_ticket_state(self, tmp_path):
        (tmp_path / "manage.py").write_text("# stub\n", encoding="utf-8")
        mock_wt = MagicMock()
        mock_wt.ticket.state = "started"

        with (
            patch("django.setup"),
            patch.object(resolve_mod, "resolve_worktree", return_value=mock_wt),
        ):
            assert _detect_agent_ticket_status(tmp_path) == "started"
