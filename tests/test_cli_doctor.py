"""Tests for doctor-related CLI commands, extracted from test_cli.py."""

import json
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

import teatree.agents.skill_bundle as teatree_skill_bundle
import teatree.cli.doctor as teatree_cli_doctor
import teatree.config as teatree_config
import teatree.core.overlay_loader as teatree_overlay_loader
from teatree.cli import app
from teatree.cli.doctor import DoctorService, IntrospectionHelpers

runner = CliRunner()


class TestDoctorService:
    """Tests for DoctorService methods (show_info, collect_overlay_skills, repair_symlinks, check_editable_sanity)."""

    # ── show_info ────────────────────────────────────────────────────

    def test_show_info_with_overlay(self, capsys):
        from teatree.config import OverlayEntry  # noqa: PLC0415

        active = OverlayEntry(name="acme", overlay_class="acme.overlay.AcmeOverlay")
        entries = [OverlayEntry(name="acme", overlay_class="acme.overlay.AcmeOverlay")]

        with (
            patch("shutil.which", return_value="/usr/bin/t3"),
            patch.object(IntrospectionHelpers, "editable_info", return_value=(False, "")),
            patch.object(IntrospectionHelpers, "print_package_info"),
            patch.object(teatree_config, "discover_active_overlay", return_value=active),
            patch.object(teatree_config, "discover_overlays", return_value=entries),
        ):
            DoctorService.show_info()

    def test_show_info_no_overlay(self, capsys):
        with (
            patch("shutil.which", return_value=None),
            patch.object(IntrospectionHelpers, "editable_info", return_value=(False, "")),
            patch.object(IntrospectionHelpers, "print_package_info"),
            patch.object(teatree_config, "discover_active_overlay", return_value=None),
            patch.object(teatree_config, "discover_overlays", return_value=[]),
        ):
            DoctorService.show_info()

    # ── collect_overlay_skills ───────────────────────────────────────

    def test_returns_overlay_skills_from_skills_dir(self, tmp_path):
        """Overlay skills are collected from projects' skills/ dirs."""
        from teatree.config import OverlayEntry  # noqa: PLC0415

        project = tmp_path / "my-project"
        skill = project / "skills" / "custom"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").touch()

        entry = OverlayEntry(name="test", overlay_class="test.overlay.TestOverlay", project_path=project)
        with patch.object(teatree_config, "discover_overlays", return_value=[entry]):
            results = DoctorService.collect_overlay_skills()
            assert len(results) == 1
            assert results[0][1] == "custom"

    def test_returns_legacy_overlay_skills(self, tmp_path):
        """Overlay skills from legacy convention (subdir with SKILL.md)."""
        from teatree.config import OverlayEntry  # noqa: PLC0415

        project = tmp_path / "my-overlay"
        project.mkdir()
        overlay_subdir = project / "my_app"
        overlay_subdir.mkdir()
        (overlay_subdir / "SKILL.md").touch()

        entry = OverlayEntry(name="my-overlay", overlay_class="test.overlay.TestOverlay", project_path=project)
        with patch.object(teatree_config, "discover_overlays", return_value=[entry]):
            results = DoctorService.collect_overlay_skills()
            assert len(results) == 1
            assert results[0][1] == "my-overlay"

    def test_returns_empty_when_no_project_path(self):
        from teatree.config import OverlayEntry  # noqa: PLC0415

        entry = OverlayEntry(name="test", overlay_class="test.overlay.TestOverlay", project_path=None)
        with patch.object(teatree_config, "discover_overlays", return_value=[entry]):
            results = DoctorService.collect_overlay_skills()
            assert results == []

    # ── repair_symlinks ──────────────────────────────────────────────

    def test_creates_links(self, tmp_path):
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        (skills_dir / "code").mkdir()
        (skills_dir / "code" / "SKILL.md").touch()

        claude_skills = tmp_path / "claude_skills"
        claude_skills.mkdir()

        with patch.object(DoctorService, "collect_overlay_skills", return_value=[]):
            created, fixed = DoctorService.repair_symlinks(skills_dir, claude_skills)
            assert created == 1
            assert fixed == 0
            assert (claude_skills / "code").is_symlink()

    def test_handles_empty_skills_dir(self, tmp_path):
        """_repair_symlinks handles empty skills dir (no SKILL.md files)."""
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        # Dir with no SKILL.md inside
        (skills_dir / "not-a-skill").mkdir()

        claude_skills = tmp_path / "claude_skills"
        claude_skills.mkdir()

        with patch.object(DoctorService, "collect_overlay_skills", return_value=[]):
            created, fixed = DoctorService.repair_symlinks(skills_dir, claude_skills)
            assert created == 0
            assert fixed == 0

    def test_fixes_wrong_target(self, tmp_path):
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        skill = skills_dir / "code"
        skill.mkdir()
        (skill / "SKILL.md").touch()

        claude_skills = tmp_path / "claude_skills"
        claude_skills.mkdir()
        # Create a symlink with wrong target
        wrong_target = tmp_path / "wrong"
        wrong_target.mkdir()
        (claude_skills / "code").symlink_to(wrong_target)

        with patch.object(DoctorService, "collect_overlay_skills", return_value=[]):
            created, fixed = DoctorService.repair_symlinks(skills_dir, claude_skills)
            assert created == 1  # re-created after unlinking
            assert fixed == 1

    def test_skips_real_dir(self, tmp_path):
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        skill = skills_dir / "code"
        skill.mkdir()
        (skill / "SKILL.md").touch()

        claude_skills = tmp_path / "claude_skills"
        claude_skills.mkdir()
        # A real directory, not a symlink
        (claude_skills / "code").mkdir()

        with patch.object(DoctorService, "collect_overlay_skills", return_value=[]):
            created, fixed = DoctorService.repair_symlinks(skills_dir, claude_skills)
            assert created == 0
            assert fixed == 0

    def test_leaves_correct_link_unchanged(self, tmp_path):
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        skill = skills_dir / "code"
        skill.mkdir()
        (skill / "SKILL.md").touch()

        claude_skills = tmp_path / "claude_skills"
        claude_skills.mkdir()
        (claude_skills / "code").symlink_to(skill)

        with patch.object(DoctorService, "collect_overlay_skills", return_value=[]):
            created, fixed = DoctorService.repair_symlinks(skills_dir, claude_skills)
            assert created == 0
            assert fixed == 0

    # ── check_editable_sanity ────────────────────────────────────────

    def test_returns_empty_when_no_settings(self, monkeypatch):
        """Returns empty when no settings module configured."""
        monkeypatch.delenv("DJANGO_SETTINGS_MODULE", raising=False)
        with patch.object(teatree_config, "discover_active_overlay", return_value=None):
            result = DoctorService.check_editable_sanity()
            assert result == []

    def test_sets_dsm_from_active_overlay(self, monkeypatch):
        """Sets DJANGO_SETTINGS_MODULE from active overlay when not in env."""
        from teatree.config import OverlayEntry  # noqa: PLC0415

        monkeypatch.delenv("DJANGO_SETTINGS_MODULE", raising=False)
        active = OverlayEntry(name="test", overlay_class="tests.teatree_core.conftest.CommandOverlay")
        with (
            patch.object(teatree_config, "discover_active_overlay", return_value=active),
            patch.object(IntrospectionHelpers, "editable_info", return_value=(False, "")),
        ):
            result = DoctorService.check_editable_sanity()
            assert isinstance(result, list)

    def test_returns_empty_when_django_fails(self, monkeypatch):
        """Returns empty when Django setup fails."""
        monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "nonexistent.settings")
        with patch("django.setup", side_effect=Exception("bad setup")):
            result = DoctorService.check_editable_sanity()
            assert result == []

    def test_warns_teatree_should_be_editable(self, monkeypatch):
        """Warns when TEATREE_EDITABLE=True but teatree is not editable."""
        monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "tests.django_settings")
        mock_settings = MagicMock()
        mock_settings.TEATREE_EDITABLE = True

        with (
            patch("django.setup"),
            patch("django.conf.settings", mock_settings),
            patch.object(IntrospectionHelpers, "editable_info", return_value=(False, "")),
            patch.object(teatree_overlay_loader, "get_all_overlays", return_value={}),
        ):
            result = DoctorService.check_editable_sanity()
            assert any("TEATREE_EDITABLE=True" in p for p in result)

    def test_warns_teatree_unexpectedly_editable(self, monkeypatch):
        """Warns when teatree is editable but not declared."""
        monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "tests.django_settings")
        mock_settings = MagicMock()
        mock_settings.TEATREE_EDITABLE = False

        with (
            patch("django.setup"),
            patch("django.conf.settings", mock_settings),
            patch.object(IntrospectionHelpers, "editable_info", return_value=(True, "file:///src")),
            patch.object(teatree_overlay_loader, "get_all_overlays", return_value={}),
        ):
            result = DoctorService.check_editable_sanity()
            assert any("TEATREE_EDITABLE is not set" in p for p in result)

    def test_warns_overlay_should_be_editable(self, monkeypatch):
        """Warns when OVERLAY_EDITABLE=True but overlay is not editable."""
        monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "tests.django_settings")
        mock_settings = MagicMock()
        mock_settings.TEATREE_EDITABLE = False
        mock_settings.OVERLAY_EDITABLE = True

        mock_overlay = MagicMock()
        mock_overlay.__module__ = "my_overlay.overlay"

        def editable_info(dist_name):
            if dist_name == "teatree":
                return (False, "")
            return (False, "")

        with (
            patch("django.setup"),
            patch("django.conf.settings", mock_settings),
            patch.object(IntrospectionHelpers, "editable_info", side_effect=editable_info),
            patch.object(teatree_overlay_loader, "get_all_overlays", return_value={"test": mock_overlay}),
            patch("importlib.metadata.packages_distributions", return_value={"my_overlay": ["my-overlay"]}),
        ):
            result = DoctorService.check_editable_sanity()
            assert any("OVERLAY_EDITABLE=True" in p for p in result)

    def test_warns_overlay_unexpectedly_editable(self, monkeypatch):
        """Warns when overlay is editable but not declared."""
        monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "tests.django_settings")
        mock_settings = MagicMock()
        mock_settings.TEATREE_EDITABLE = False
        mock_settings.OVERLAY_EDITABLE = False

        mock_overlay = MagicMock()
        mock_overlay.__module__ = "my_overlay.overlay"

        def editable_info(dist_name):
            if dist_name == "teatree":
                return (False, "")
            return (True, "file:///src")

        with (
            patch("django.setup"),
            patch("django.conf.settings", mock_settings),
            patch.object(IntrospectionHelpers, "editable_info", side_effect=editable_info),
            patch.object(teatree_overlay_loader, "get_all_overlays", return_value={"test": mock_overlay}),
            patch("importlib.metadata.packages_distributions", return_value={"my_overlay": ["my-overlay"]}),
        ):
            result = DoctorService.check_editable_sanity()
            assert any("OVERLAY_EDITABLE is not set" in p for p in result)

    def test_no_warnings_when_editable_state_matches(self, monkeypatch):
        """No warnings when editable state matches declared intent."""
        monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "tests.django_settings")
        mock_settings = MagicMock()
        mock_settings.TEATREE_EDITABLE = False
        mock_settings.OVERLAY_EDITABLE = False

        mock_overlay = MagicMock()
        mock_overlay.__module__ = "my_overlay.overlay"

        with (
            patch("django.setup"),
            patch("django.conf.settings", mock_settings),
            patch.object(IntrospectionHelpers, "editable_info", return_value=(False, "")),
            patch.object(teatree_overlay_loader, "get_all_overlays", return_value={"test": mock_overlay}),
            patch("importlib.metadata.packages_distributions", return_value={"my_overlay": ["my-overlay"]}),
        ):
            result = DoctorService.check_editable_sanity()
            assert result == []


class TestIntrospectionHelpers:
    """Tests for IntrospectionHelpers methods (print_package_info, editable_info)."""

    # ── editable_info ────────────────────────────────────────────────

    def test_not_installed(self):
        from importlib.metadata import PackageNotFoundError  # noqa: PLC0415

        with patch("importlib.metadata.distribution", side_effect=PackageNotFoundError("x")):
            assert IntrospectionHelpers.editable_info("nonexistent") == (False, "")

    def test_no_direct_url(self):
        mock_dist = MagicMock()
        mock_dist.read_text.return_value = None
        with patch("importlib.metadata.distribution", return_value=mock_dist):
            assert IntrospectionHelpers.editable_info("some-pkg") == (False, "")

    def test_editable(self):
        mock_dist = MagicMock()
        mock_dist.read_text.return_value = json.dumps(
            {
                "dir_info": {"editable": True},
                "url": "file:///home/user/project",
            }
        )
        with patch("importlib.metadata.distribution", return_value=mock_dist):
            editable, url = IntrospectionHelpers.editable_info("some-pkg")
            assert editable is True
            assert url == "file:///home/user/project"

    def test_invalid_json(self):
        mock_dist = MagicMock()
        mock_dist.read_text.return_value = "not json"
        with patch("importlib.metadata.distribution", return_value=mock_dist):
            assert IntrospectionHelpers.editable_info("some-pkg") == (False, "")

    # ── print_package_info ───────────────────────────────────────────

    def test_installed(self, capsys):
        with (
            patch("importlib.import_module") as mock_import,
            patch.object(IntrospectionHelpers, "editable_info", return_value=(False, "")),
        ):
            mock_mod = MagicMock()
            mock_mod.__file__ = "/usr/lib/python/teatree/__init__.py"
            mock_import.return_value = mock_mod
            IntrospectionHelpers.print_package_info("teatree", "teatree")
            # Just verifying it runs without error; output goes through typer.echo

    def test_not_installed_package(self, capsys):
        with patch("importlib.import_module", side_effect=ImportError("nope")):
            IntrospectionHelpers.print_package_info("teatree", "teatree")
            # Verifying it handles ImportError gracefully

    def test_editable_with_url(self, capsys):
        with (
            patch("importlib.import_module") as mock_import,
            patch.object(IntrospectionHelpers, "editable_info", return_value=(True, "file:///src")),
        ):
            mock_mod = MagicMock()
            mock_mod.__file__ = "/src/teatree/__init__.py"
            mock_import.return_value = mock_mod
            IntrospectionHelpers.print_package_info("teatree", "teatree")

    def test_editable_no_url(self, capsys):
        """_print_package_info doesn't print URL when editable but no url."""
        with (
            patch("importlib.import_module") as mock_import,
            patch.object(IntrospectionHelpers, "editable_info", return_value=(True, "")),
        ):
            mock_mod = MagicMock()
            mock_mod.__file__ = "/src/teatree/__init__.py"
            mock_import.return_value = mock_mod
            IntrospectionHelpers.print_package_info("teatree", "teatree")


class TestDoctorCommands:
    """Tests for CLI command wrappers (using CliRunner)."""

    # ── repair ───────────────────────────────────────────────────────

    def test_repair(self, tmp_path, monkeypatch):
        """Doctor repair creates/fixes symlinks."""
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        (skills_dir / "code").mkdir()
        (skills_dir / "code" / "SKILL.md").touch()

        claude_skills = tmp_path / "claude_skills"
        claude_skills.mkdir()
        # Create a broken symlink
        broken = claude_skills / "broken-link"
        broken.symlink_to(tmp_path / "nonexistent")

        with (
            patch.object(teatree_skill_bundle, "DEFAULT_SKILLS_DIR", skills_dir),
            patch("pathlib.Path.home", return_value=tmp_path),
            patch.object(DoctorService, "collect_overlay_skills", return_value=[]),
        ):
            # Create the .claude/skills dir where the command expects it
            real_claude_skills = tmp_path / ".claude" / "skills"
            real_claude_skills.mkdir(parents=True)
            # Add a broken symlink
            broken2 = real_claude_skills / "broken-link"
            broken2.symlink_to(tmp_path / "nonexistent2")

            result = runner.invoke(app, ["doctor", "repair"])
            assert result.exit_code == 0
            assert "Skills:" in result.output

    def test_repair_no_skills_dir(self, tmp_path):
        """Doctor repair fails when skills dir not found."""
        with patch.object(teatree_skill_bundle, "DEFAULT_SKILLS_DIR", tmp_path / "nonexistent"):
            result = runner.invoke(app, ["doctor", "repair"])
            assert result.exit_code == 1
            assert "Skills directory not found" in result.output

    def test_repair_with_overlay_skills(self, tmp_path):
        """Repair reports overlay skill count."""
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        (skills_dir / "core").mkdir()
        (skills_dir / "core" / "SKILL.md").touch()

        overlay_skill = tmp_path / "overlay-skill"
        overlay_skill.mkdir()
        (overlay_skill / "SKILL.md").touch()

        with (
            patch.object(teatree_skill_bundle, "DEFAULT_SKILLS_DIR", skills_dir),
            patch("pathlib.Path.home", return_value=tmp_path),
            patch.object(DoctorService, "collect_overlay_skills", return_value=[(overlay_skill, "t3-overlay")]),
        ):
            claude_skills = tmp_path / ".claude" / "skills"
            claude_skills.mkdir(parents=True)

            result = runner.invoke(app, ["doctor", "repair"])
            assert result.exit_code == 0
            assert "overlay skill(s)" in result.output

    # ── check ────────────────────────────────────────────────────────

    def test_check_ok(self):
        """Doctor check passes when all checks pass."""
        with (
            patch.object(DoctorService, "check_editable_sanity", return_value=[]),
        ):
            result = runner.invoke(app, ["doctor", "check"])
            assert result.exit_code == 0
            assert "All checks passed" in result.output

    def test_check_with_warnings(self):
        """Doctor check shows warnings."""
        with patch.object(
            DoctorService,
            "check_editable_sanity",
            return_value=["teatree is editable but not declared"],
        ):
            result = runner.invoke(app, ["doctor", "check"])
            assert result.exit_code == 0
            assert "WARN" in result.output

    def test_check_fails_when_required_tool_missing(self):
        """Doctor check fails when a required tool is not on PATH."""
        with (
            patch.object(
                teatree_cli_doctor.shutil,
                "which",
                side_effect=lambda t: None if t == "direnv" else f"/usr/bin/{t}",
            ),
            patch.object(DoctorService, "check_editable_sanity", return_value=[]),
        ):
            result = runner.invoke(app, ["doctor", "check"])
            assert result.exit_code == 0  # typer returns 0; check() returns bool
            assert "FAIL  Required tool not found: direnv" in result.output

    def test_check_import_failure(self):
        """Doctor check returns False on import failure."""
        import builtins  # noqa: PLC0415

        real_import = builtins.__import__

        def fail_import(name, *args, **kwargs):
            if name == "teatree.core":
                raise ImportError(name)
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=fail_import):
            result = runner.invoke(app, ["doctor", "check"])
            assert "FAIL" in result.output

    # ── info ─────────────────────────────────────────────────────────

    def test_info(self):
        """Doctor info delegates to _show_info."""
        with (
            patch("shutil.which", return_value="/usr/local/bin/t3"),
            patch.object(IntrospectionHelpers, "editable_info", return_value=(False, "")),
            patch.object(IntrospectionHelpers, "print_package_info"),
            patch.object(teatree_config, "discover_active_overlay", return_value=None),
            patch.object(teatree_config, "discover_overlays", return_value=[]),
        ):
            result = runner.invoke(app, ["doctor", "info"])
            assert result.exit_code == 0
