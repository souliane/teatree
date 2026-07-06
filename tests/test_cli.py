"""Tests for teatree.cli — comprehensive CLI command coverage.

Uses typer.testing.CliRunner to invoke commands and mocks external
dependencies (subprocess, filesystem, network, Django).
"""

import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from django.test import TestCase
from typer.testing import CliRunner

import teatree.agents.skill_bundle as skill_bundle_mod
import teatree.claude_sessions as claude_sessions_mod
import teatree.cli as cli_mod
import teatree.cli.agent as cli_agent_mod
import teatree.cli.review.request as cli_review_request_mod
import teatree.config as config_mod
import teatree.core.intake.resolve as resolve_mod
import teatree.core.overlay_loader as overlay_loader_mod
import teatree.utils.run as utils_run_mod
from teatree.cli import _ensure_editable_if_contributing, _find_overlay_project, _find_project_root, app
from teatree.cli import doctor as cli_doctor_mod
from teatree.cli.agent import _detect_agent_ticket_status
from teatree.cli.doctor import DoctorService, IntrospectionHelpers
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

        # ``docs`` runs mkdocs through ``run_streamed`` → ``Popen`` (tee
        # stderr, then ``wait()``); mock that seam, not ``subprocess.run``.
        proc = MagicMock()
        proc.stderr = iter(())
        proc.wait.return_value = 0
        ctx = MagicMock()
        ctx.__enter__.return_value = proc
        ctx.__exit__.return_value = False
        mock_run = MagicMock(return_value=ctx)
        with (
            patch.object(utils_run_mod, "Popen", mock_run),
            patch("teatree.cli._maybe_show_update_notice"),
        ):
            result = runner.invoke(app, ["docs"])
            assert result.exit_code == 0
            mock_run.assert_called_once()
            assert "mkdocs" in str(mock_run.call_args)


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
        from teatree.skill_support.loading import SkillLoadingPolicy, SkillSelectionResult  # noqa: PLC0415

        mock_overlay = OverlayEntry(name="test-overlay", overlay_class="test.overlay.TestOverlay")
        overlay_obj = MagicMock()
        overlay_obj.metadata.get_skill_metadata.return_value = {"skill_path": "skills/test/SKILL.md"}

        with (
            patch.object(config_mod, "discover_active_overlay", return_value=mock_overlay),
            patch.object(overlay_loader_mod, "get_overlay", return_value=overlay_obj),
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch.object(cli_doctor_mod.IntrospectionHelpers, "editable_info", return_value=(False, "")),
            patch.object(cli_agent_mod, "_detect_agent_ticket_status", return_value="started"),
            patch.object(
                SkillLoadingPolicy,
                "select_for_agent_launch",
                return_value=SkillSelectionResult(skills=["code"]),
            ),
            patch.object(cli_agent_mod.os, "execvp") as mock_exec,
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

        from teatree.skill_support.loading import SkillLoadingPolicy, SkillSelectionResult  # noqa: PLC0415

        with (
            patch.object(config_mod, "discover_active_overlay", return_value=None),
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch.object(cli_doctor_mod.IntrospectionHelpers, "editable_info", return_value=(False, "")),
            patch.object(
                SkillLoadingPolicy,
                "select_for_agent_launch",
                return_value=SkillSelectionResult(skills=["code"]),
            ),
            patch.object(cli_agent_mod.os, "execvp") as mock_exec,
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

        from teatree.skill_support.loading import SkillLoadingPolicy  # noqa: PLC0415

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

    def test_info_lists_overlays(self):
        """Info command includes installed overlays, replacing the removed top-level `overlays` command."""
        from teatree.config import OverlayEntry  # noqa: PLC0415

        entries = [
            OverlayEntry(name="acme", overlay_class="acme.overlay.AcmeOverlay"),
            OverlayEntry(name="demo", overlay_class="demo.overlay.DemoOverlay"),
        ]
        active = OverlayEntry(name="acme", overlay_class="acme.overlay.AcmeOverlay")
        with (
            patch("shutil.which", return_value="/usr/local/bin/t3"),
            patch.object(cli_doctor_mod.IntrospectionHelpers, "editable_info", return_value=(True, "file:///home/src")),
            patch.object(cli_doctor_mod.IntrospectionHelpers, "print_package_info"),
            patch.object(config_mod, "discover_active_overlay", return_value=active),
            patch.object(config_mod, "discover_overlays", return_value=entries),
        ):
            result = runner.invoke(app, ["info"])
            assert result.exit_code == 0
            assert "acme" in result.output
            assert "demo" in result.output

    def test_info_artifacts_subcommand_is_registered(self):
        """``t3 info artifacts`` is reachable as a subcommand (#273)."""
        result = runner.invoke(app, ["info", "artifacts", "--help"])
        assert result.exit_code == 0
        assert "ticket" in result.output.lower()

    def test_info_artifacts_rejects_unknown_format(self):
        """The CLI wrapper rejects an unknown ``--format`` with exit 2 (#273)."""
        result = runner.invoke(app, ["info", "artifacts", "1", "--format", "yaml"])
        assert result.exit_code == 2


# ── config subcommands ────────────────────────────────────────────────


class TestConfigCommands:
    def test_write_skill_cache_writes_json(self, tmp_path, monkeypatch):
        """write-skill-cache delegates to the canonical write_skill_metadata_cache helper."""
        import teatree.core.skill_cache as startup_mod  # noqa: PLC0415
        from teatree.config import OverlayEntry  # noqa: PLC0415

        active = OverlayEntry(name="test", overlay_class="test.overlay.TestOverlay")
        mock_overlay = MagicMock()
        mock_overlay.metadata.get_skill_metadata.return_value = {"skill_path": "skills/test/SKILL.md"}

        monkeypatch.setattr(startup_mod, "DATA_DIR", tmp_path)
        monkeypatch.delenv("DJANGO_SETTINGS_MODULE", raising=False)
        with (
            patch.object(config_mod, "discover_active_overlay", return_value=active),
            patch("django.setup"),
            patch.object(startup_mod, "get_overlay", return_value=mock_overlay),
            patch.object(startup_mod, "_build_requires_index", return_value=[]),
            patch.object(startup_mod, "resolve_all", return_value={}),
            patch.object(startup_mod, "_collect_skill_mtimes", return_value={}),
        ):
            result = runner.invoke(app, ["config", "write-skill-cache"])
            assert result.exit_code == 0
            assert "Wrote skill metadata" in result.output
            cache = tmp_path / "skill-metadata.json"
            assert cache.is_file()
            data = json.loads(cache.read_text())
            assert data["skill_path"] == "skills/test/SKILL.md"
            assert data["skill_index"] == []
            assert "teatree_version" in data

    def test_write_skill_cache_no_active_overlay(self, tmp_path, monkeypatch):
        """write-skill-cache works when DJANGO_SETTINGS_MODULE is already set."""
        import teatree.core.skill_cache as startup_mod  # noqa: PLC0415

        mock_overlay = MagicMock()
        mock_overlay.metadata.get_skill_metadata.return_value = {}

        monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "tests.django_settings")
        monkeypatch.setattr(startup_mod, "DATA_DIR", tmp_path)
        with (
            patch.object(config_mod, "discover_active_overlay", return_value=None),
            patch("django.setup"),
            patch.object(startup_mod, "get_overlay", return_value=mock_overlay),
            patch.object(startup_mod, "_build_requires_index", return_value=[]),
            patch.object(startup_mod, "resolve_all", return_value={}),
            patch.object(startup_mod, "_collect_skill_mtimes", return_value={}),
        ):
            result = runner.invoke(app, ["config", "write-skill-cache"])
            assert result.exit_code == 0

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
        monkeypatch.setattr("teatree.paths.DATA_DIR", tmp_path)
        cache_path = tmp_path / "skill-metadata.json"
        cache_path.write_text('{"skill_path": "skills/test/SKILL.md"}\n')

        result = runner.invoke(app, ["config", "cache"])
        assert result.exit_code == 0
        assert "skill_path" in result.output

    def test_cache_no_file(self, tmp_path, monkeypatch):
        """Config cache fails when no cache file exists."""
        monkeypatch.setattr("teatree.paths.DATA_DIR", tmp_path)

        result = runner.invoke(app, ["config", "cache"])
        assert result.exit_code == 1
        assert "No cache found" in result.output

    def test_deps_shows_chain(self, tmp_path, monkeypatch):
        """Config deps shows resolved dependency chain from cache."""
        monkeypatch.setattr("teatree.paths.DATA_DIR", tmp_path)
        cache = tmp_path / "skill-metadata.json"
        cache.write_text(
            json.dumps(
                {
                    "skill_index": [
                        {"skill": "rules", "requires": []},
                        {"skill": "workspace", "requires": ["rules"]},
                        {"skill": "code", "requires": ["workspace"]},
                    ],
                    "resolved_requires": {
                        "rules": ["rules"],
                        "workspace": ["rules", "workspace"],
                        "code": ["rules", "workspace", "code"],
                    },
                },
            ),
        )
        result = runner.invoke(app, ["config", "deps", "code"])
        assert result.exit_code == 0
        assert "rules → workspace → code" in result.output

    def test_deps_no_cache(self, tmp_path, monkeypatch):
        """Config deps fails when no cache exists."""
        monkeypatch.setattr("teatree.paths.DATA_DIR", tmp_path)
        result = runner.invoke(app, ["config", "deps", "test"])
        assert result.exit_code == 1
        assert "No cache found" in result.output

    def test_deps_computes_when_not_precomputed(self, tmp_path, monkeypatch):
        """Config deps computes deps on the fly if resolved_requires is missing."""
        monkeypatch.setattr("teatree.paths.DATA_DIR", tmp_path)
        cache = tmp_path / "skill-metadata.json"
        cache.write_text(
            json.dumps(
                {
                    "skill_index": [
                        {"skill": "rules", "requires": []},
                        {"skill": "workspace", "requires": ["rules"]},
                    ],
                },
            ),
        )
        result = runner.invoke(app, ["config", "deps", "workspace"])
        assert result.exit_code == 0
        assert "rules → workspace" in result.output


# ── Review-request discover ──────────────────────────────────────────


class TestReviewRequestDiscover:
    def test_review_request_discover(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "pyproject.toml").write_text("[project]\n")

        from teatree.config import OverlayEntry  # noqa: PLC0415

        active = OverlayEntry(name="t3-test", overlay_class="test.Overlay", project_path=tmp_path)
        with (
            patch.object(config_mod, "_active_overlay_entry", return_value=active),
            patch.object(cli_review_request_mod, "managepy_core") as mock_manage,
        ):
            result = runner.invoke(app, ["review-request", "discover"])
            assert result.exit_code == 0
            mock_manage.assert_called_once_with("followup", "discover-mrs", overlay_name="t3-test")

    def test_review_request_check(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "pyproject.toml").write_text("[project]\n")

        from teatree.config import OverlayEntry  # noqa: PLC0415

        active = OverlayEntry(name="t3-test", overlay_class="test.Overlay", project_path=tmp_path)
        mr = "https://gitlab.com/org/repo/-/merge_requests/385"
        with (
            patch.object(config_mod, "_active_overlay_entry", return_value=active),
            patch.object(cli_review_request_mod, "managepy_core") as mock_manage,
        ):
            result = runner.invoke(app, ["review-request", "check", "--mr-url", mr])
            assert result.exit_code == 0
            mock_manage.assert_called_once_with(
                "review_request_check",
                "--mr-url",
                mr,
                overlay_name="t3-test",
            )


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
        skill_dir = tmp_path / "skills" / "t3:acme"
        s = OverlayScaffolder(tmp_path, "t3_overlay", "pkg")
        s.write_skill_md(skill_dir, "t3-acme", "t3:acme")
        text = (skill_dir / "SKILL.md").read_text()
        assert "name: t3:acme" in text
        assert "t3:workspace" not in text

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
                cli_doctor_mod.IntrospectionHelpers,
                "editable_info",
                return_value=(True, "file:///src/teatree"),
            ),
            patch.object(cli_agent_mod.os, "execvp") as mock_exec,
        ):
            from teatree.cli.agent import _launch_claude  # noqa: PLC0415

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
            patch.object(cli_agent_mod.os, "execvp") as mock_exec,
        ):
            from teatree.cli.agent import _launch_claude  # noqa: PLC0415

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
            patch.object(cli_agent_mod.os, "execvp") as mock_exec,
        ):
            from teatree.cli.agent import _launch_claude  # noqa: PLC0415

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


class TestLaunchClaudeContributeDbHome(TestCase):
    """``--plugin-dir`` is gated on the DB-home ``contribute_plugin_dir`` (#2697)."""

    @pytest.fixture(autouse=True)
    def _stage(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp_path = tmp_path
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("T3_CONTRIBUTE", raising=False)
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)
        (tmp_path / "pyproject.toml").write_text("[project]\n")

    def _launch_and_get_cmd(self) -> list[str]:
        from teatree.cli.agent import _launch_claude  # noqa: PLC0415

        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch.object(cli_doctor_mod.IntrospectionHelpers, "editable_info", return_value=(False, "")),
            patch.object(cli_agent_mod.os, "execvp") as mock_exec,
        ):
            _launch_claude(
                task="",
                project_root=self.tmp_path,
                context_lines=["test"],
                skills=[],
                ask_user_which_skill=False,
            )
            return mock_exec.call_args[0][1]

    def test_plugin_dir_added_when_db_contribute_plugin_dir_on(self) -> None:
        from teatree.core.models import ConfigSetting  # noqa: PLC0415

        ConfigSetting.objects.set_value("contribute_plugin_dir", value=True)
        assert "--plugin-dir" in self._launch_and_get_cmd()

    def test_no_plugin_dir_when_contribute_unset(self) -> None:
        assert "--plugin-dir" not in self._launch_and_get_cmd()


# ── _detect_agent_ticket_status ──────────────────────────────────────


class TestDetectAgentTicketStatus:
    def test_returns_empty_without_manage_py(self, tmp_path):
        assert _detect_agent_ticket_status(tmp_path) == ""

    def test_returns_error_on_exception(self, tmp_path):
        (tmp_path / "manage.py").write_text("# stub\n", encoding="utf-8")

        with patch("django.setup", side_effect=Exception("boom")):
            assert _detect_agent_ticket_status(tmp_path) == "(error)"

    def test_returns_ticket_state(self, tmp_path):
        (tmp_path / "manage.py").write_text("# stub\n", encoding="utf-8")
        mock_wt = MagicMock()
        mock_wt.ticket.state = "started"

        with (
            patch("django.setup"),
            patch.object(resolve_mod, "resolve_worktree", return_value=mock_wt),
        ):
            assert _detect_agent_ticket_status(tmp_path) == "started"


class TestCheckUpdateCommand:
    def test_shows_update_message(self) -> None:
        with patch.object(config_mod, "check_for_updates", return_value="New version 1.2.3 available"):
            result = runner.invoke(app, ["config", "check-update"])
        assert result.exit_code == 0
        assert "New version 1.2.3 available" in result.output

    def test_shows_up_to_date(self) -> None:
        with patch.object(config_mod, "check_for_updates", return_value=None):
            result = runner.invoke(app, ["config", "check-update"])
        assert result.exit_code == 0
        assert "You are up to date" in result.output


class TestMaybeShowUpdateNotice:
    def test_shows_notice_on_stderr(self, capsys, monkeypatch) -> None:
        monkeypatch.setattr("sys.argv", ["t3", "info"])
        with patch.object(config_mod, "check_for_updates", return_value="Update available"):
            cli_mod._maybe_show_update_notice()
        captured = capsys.readouterr()
        assert "Update available" in captured.err
        assert captured.out == ""

    def test_suppresses_exceptions(self, monkeypatch) -> None:
        monkeypatch.setattr("sys.argv", ["t3", "info"])
        with patch.object(config_mod, "check_for_updates", side_effect=RuntimeError("boom")):
            cli_mod._maybe_show_update_notice()  # should not raise

    def test_suppressed_in_json_mode(self, capsys, monkeypatch) -> None:
        """The human banner must never pollute machine-readable output (#719)."""
        monkeypatch.setattr("sys.argv", ["t3", "ci", "coverage", "--json"])
        with patch.object(config_mod, "check_for_updates", return_value="Update available"):
            cli_mod._maybe_show_update_notice()
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""

    def test_json_eq_form_also_suppressed(self, capsys, monkeypatch) -> None:
        monkeypatch.setattr("sys.argv", ["t3", "tool", "audit-memory", "--json=true"])
        with patch.object(config_mod, "check_for_updates", return_value="Update available"):
            cli_mod._maybe_show_update_notice()
        captured = capsys.readouterr()
        assert captured.err == ""


class TestUpdateNoticeDoesNotPolluteJsonStdout:
    """End-to-end: `t3 ci coverage --json` stdout must be valid JSON (#719)."""

    def test_ci_coverage_json_is_parseable_with_update_available(self, monkeypatch, tmp_path) -> None:
        import teatree.cli.ci as ci_mod  # noqa: PLC0415
        from teatree.utils.coverage_floor import CoverageReport, ModuleCoverage  # noqa: PLC0415

        monkeypatch.chdir(tmp_path)
        (tmp_path / "pyproject.toml").write_text("[tool.coverage.report]\nfail_under = 93\n", encoding="utf-8")
        fake = CoverageReport(
            overall_percent=95.0,
            overall_floor=93,
            module_results=[ModuleCoverage(path="x.py", floor=80, percent=85.0)],
        )
        with (
            patch.object(config_mod, "check_for_updates", return_value="Update available"),
            patch.object(ci_mod, "measure_coverage", return_value=fake),
        ):
            result = runner.invoke(app, ["ci", "coverage", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["overall_percent"] == pytest.approx(95.0)


class TestEnsureEditableIfContributing:
    # ``contribute`` is DB-home (#1775): ``_ensure_editable_if_contributing``
    # resolves it via ``get_effective_settings()``, so the patch targets that
    # tier (not ``load_config``, whose ``.user.contribute`` is now ignored).
    def test_skips_when_contribute_false(self) -> None:
        with patch.object(config_mod, "get_effective_settings", return_value=MagicMock(contribute=False)):
            _ensure_editable_if_contributing()
        # Should return early without calling editable_info

    def test_makes_teatree_editable(self) -> None:
        with (
            patch.object(config_mod, "get_effective_settings", return_value=MagicMock(contribute=True)),
            patch.object(IntrospectionHelpers, "editable_info", return_value=(False, "")),
            patch.object(DoctorService, "find_teatree_repo", return_value=Path("/fake/teatree")),
            patch.object(DoctorService, "make_editable") as mock_make,
            patch.object(overlay_loader_mod, "get_all_overlays", return_value={}),
        ):
            _ensure_editable_if_contributing()
        mock_make.assert_called_once_with("teatree", Path("/fake/teatree"))

    def test_skips_teatree_when_already_editable(self) -> None:
        with (
            patch.object(config_mod, "get_effective_settings", return_value=MagicMock(contribute=True)),
            patch.object(IntrospectionHelpers, "editable_info", return_value=(True, "/fake")),
            patch.object(DoctorService, "find_teatree_repo") as mock_find,
            patch.object(overlay_loader_mod, "get_all_overlays", return_value={}),
        ):
            _ensure_editable_if_contributing()
        mock_find.assert_not_called()

    def test_makes_overlay_editable(self) -> None:
        mock_overlay = MagicMock()
        type(mock_overlay).__module__ = "myoverlay.overlay"

        with (
            patch.object(config_mod, "get_effective_settings", return_value=MagicMock(contribute=True)),
            patch.object(
                IntrospectionHelpers,
                "editable_info",
                side_effect=[(True, "/teatree"), (False, "")],
            ),
            patch.object(overlay_loader_mod, "get_all_overlays", return_value={"my": mock_overlay}),
            patch("importlib.metadata.packages_distributions", return_value={"myoverlay": ["myoverlay-dist"]}),
            patch.object(DoctorService, "find_overlay_repo", return_value=Path("/fake/overlay")),
            patch.object(DoctorService, "make_editable") as mock_make,
        ):
            _ensure_editable_if_contributing()
        mock_make.assert_called_once_with("myoverlay-dist", Path("/fake/overlay"))

    def test_suppresses_exceptions(self) -> None:
        with patch.object(config_mod, "get_effective_settings", side_effect=RuntimeError("boom")):
            _ensure_editable_if_contributing()  # should not raise


class TestRegisterOverlayCommandsCanonicalDedup:
    """A TOML overlay and its ``t3-``-prefixed entry point share one route key.

    Both ``acme`` (TOML, with a path) and ``t3-acme`` (entry point) canonicalise
    to the ``acme`` route key. Left distinct they each register an ``acme`` Typer
    sub-app — the collision. Exactly one must be registered, and the existing
    allowlist behaviour (registering ``beta`` from ``t3-beta``) is preserved.
    """

    def _registered_names(self, mock_add) -> list[str]:
        return [call.kwargs.get("name") or call.args[1] for call in mock_add.call_args_list]

    def test_toml_and_entry_point_register_exactly_one_subapp(self) -> None:
        from teatree.cli import register_overlay_commands  # noqa: PLC0415
        from teatree.config import OverlayEntry  # noqa: PLC0415

        toml_overlay = OverlayEntry(name="acme", overlay_class="", project_path=Path("/tmp/acme"))
        entry_point = OverlayEntry(name="t3-acme", overlay_class="acme_pkg.overlay:AcmeOverlay")

        with (
            patch("teatree.config.discover_overlays", return_value=[toml_overlay, entry_point]),
            patch("teatree.config.discover_active_overlay", return_value=None),
            patch("teatree.cli.OverlayAppBuilder") as mock_builder,
            patch("teatree.cli.app.add_typer") as mock_add,
        ):
            register_overlay_commands()

        assert self._registered_names(mock_add) == ["acme"]
        assert mock_builder.call_count == 1

    def test_collapse_inherits_project_path_from_toml_sibling(self) -> None:
        from teatree.cli import register_overlay_commands  # noqa: PLC0415
        from teatree.config import OverlayEntry  # noqa: PLC0415

        toml_overlay = OverlayEntry(name="acme", overlay_class="", project_path=Path("/tmp/acme"))
        entry_point = OverlayEntry(name="t3-acme", overlay_class="acme_pkg.overlay:AcmeOverlay")

        with (
            patch("teatree.config.discover_overlays", return_value=[toml_overlay, entry_point]),
            patch("teatree.config.discover_active_overlay", return_value=None),
            patch("teatree.cli.OverlayAppBuilder") as mock_builder,
            patch("teatree.cli.app.add_typer"),
        ):
            register_overlay_commands()

        entry_name, project_path, _settings = mock_builder.call_args.args
        assert entry_name == "t3-acme"
        assert project_path == Path("/tmp/acme")

    def test_allowlist_still_registers_entry_point_subapp(self) -> None:
        from teatree.cli import register_overlay_commands  # noqa: PLC0415
        from teatree.config import OverlayEntry  # noqa: PLC0415

        beta = OverlayEntry(name="t3-beta", overlay_class="beta_pkg.overlay:BetaOverlay")
        other = OverlayEntry(name="t3-other-fake", overlay_class="")

        with (
            patch("teatree.config.discover_overlays", return_value=[beta, other]),
            patch("teatree.config.discover_active_overlay", return_value=None),
            patch("teatree.cli.OverlayAppBuilder") as mock_builder,
            patch("teatree.cli.app.add_typer") as mock_add,
        ):
            register_overlay_commands(allowlist={"t3-beta"})

        assert self._registered_names(mock_add) == ["beta"]
        assert mock_builder.call_count == 1
