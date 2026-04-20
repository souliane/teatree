"""Tests for doctor-related CLI commands, extracted from test_cli.py."""

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

import teatree.cli.doctor as teatree_cli_doctor
import teatree.config as teatree_config
import teatree.core.overlay_loader as teatree_overlay_loader
from teatree.cli import app
from teatree.cli.doctor import DoctorService, IntrospectionHelpers

runner = CliRunner()


def _make_overlay_stub(module: str = "my_overlay.overlay") -> object:
    """Create a stub whose ``type().__module__`` returns *module*.

    ``_resolve_overlay_dists`` uses ``type(inst).__module__``, not
    ``inst.__module__``.  A plain MagicMock's type is ``MagicMock``
    (module ``unittest.mock``), so we create a real class instead.
    """
    cls = type("_OverlayStub", (), {"__module__": module})
    return cls()


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

    def test_ignores_legacy_overlay_convention(self, tmp_path):
        """Overlay skills only found via skills/ directory, not legacy subdir convention."""
        from teatree.config import OverlayEntry  # noqa: PLC0415

        project = tmp_path / "my-overlay"
        project.mkdir()
        overlay_subdir = project / "my_app"
        overlay_subdir.mkdir()
        (overlay_subdir / "SKILL.md").touch()

        entry = OverlayEntry(name="my-overlay", overlay_class="test.overlay.TestOverlay", project_path=project)
        with patch.object(teatree_config, "discover_overlays", return_value=[entry]):
            results = DoctorService.collect_overlay_skills()
            assert results == []

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

    def test_returns_empty_when_contribute_false_and_nothing_editable(self):
        """Returns empty when contribute=false and nothing is editable."""
        mock_config = MagicMock()
        mock_config.user.contribute = False

        with (
            patch("teatree.config.load_config", return_value=mock_config),
            patch.object(IntrospectionHelpers, "editable_info", return_value=(False, "")),
            patch.object(teatree_overlay_loader, "get_all_overlays", return_value={}),
        ):
            result = DoctorService.check_editable_sanity()
            assert result == []

    def test_returns_empty_when_contribute_true_and_all_editable(self):
        """Returns empty when contribute=true and everything is already editable."""
        mock_config = MagicMock()
        mock_config.user.contribute = True

        with (
            patch("teatree.config.load_config", return_value=mock_config),
            patch.object(IntrospectionHelpers, "editable_info", return_value=(True, "file:///src")),
            patch.object(teatree_overlay_loader, "get_all_overlays", return_value={}),
        ):
            result = DoctorService.check_editable_sanity()
            assert result == []

    def test_auto_fixes_when_contribute_true_and_not_editable(self):
        """Auto-installs editable teatree when contribute=true and repo is found."""
        mock_config = MagicMock()
        mock_config.user.contribute = True

        with (
            patch.object(IntrospectionHelpers, "editable_info", return_value=(False, "")),
            patch.object(teatree_overlay_loader, "get_all_overlays", return_value={}),
            patch("teatree.config.load_config", return_value=mock_config),
            patch.object(DoctorService, "find_teatree_repo", return_value=Path("/tmp/teatree")),
            patch.object(DoctorService, "make_editable") as mock_fix,
        ):
            result = DoctorService.check_editable_sanity()
            mock_fix.assert_called_once_with("teatree", Path("/tmp/teatree"))
            assert result == []

    def test_warns_when_contribute_true_and_repo_not_found(self):
        """Warns when contribute=true, teatree not editable, and repo path not found."""
        mock_config = MagicMock()
        mock_config.user.contribute = True

        with (
            patch.object(IntrospectionHelpers, "editable_info", return_value=(False, "")),
            patch.object(teatree_overlay_loader, "get_all_overlays", return_value={}),
            patch("teatree.config.load_config", return_value=mock_config),
            patch.object(DoctorService, "find_teatree_repo", return_value=None),
        ):
            result = DoctorService.check_editable_sanity()
            assert any("contribute=true" in p for p in result)

    def test_warns_teatree_unexpectedly_editable(self):
        """Warns when teatree is editable but contribute=false."""
        mock_config = MagicMock()
        mock_config.user.contribute = False

        with (
            patch.object(IntrospectionHelpers, "editable_info", return_value=(True, "file:///src")),
            patch.object(teatree_overlay_loader, "get_all_overlays", return_value={}),
            patch("teatree.config.load_config", return_value=mock_config),
        ):
            result = DoctorService.check_editable_sanity()
            assert any("contribute=false" in p for p in result)

    def test_auto_fixes_overlay_when_contribute_true(self):
        """Auto-installs editable overlay when contribute=true and repo is found."""
        mock_config = MagicMock()
        mock_config.user.contribute = True

        overlay_stub = _make_overlay_stub("my_overlay.overlay")

        def editable_info(dist_name):
            return (dist_name == "teatree", "")  # teatree editable, overlay not

        with (
            patch.object(IntrospectionHelpers, "editable_info", side_effect=editable_info),
            patch.object(teatree_overlay_loader, "get_all_overlays", return_value={"test": overlay_stub}),
            patch.object(teatree_cli_doctor, "packages_distributions", return_value={"my_overlay": ["my-overlay"]}),
            patch("teatree.config.load_config", return_value=mock_config),
            patch.object(DoctorService, "find_overlay_repo", return_value=Path("/tmp/my-overlay")),
            patch.object(DoctorService, "make_editable") as mock_fix,
        ):
            result = DoctorService.check_editable_sanity()
            mock_fix.assert_called_once_with("my-overlay", Path("/tmp/my-overlay"))
            assert result == []

    def test_warns_overlay_unexpectedly_editable(self):
        """Warns when overlay is editable but contribute=false."""
        mock_config = MagicMock()
        mock_config.user.contribute = False

        overlay_stub = _make_overlay_stub("my_overlay.overlay")

        def editable_info(dist_name):
            if dist_name == "teatree":
                return (False, "")
            return (True, "file:///src")

        with (
            patch.object(IntrospectionHelpers, "editable_info", side_effect=editable_info),
            patch.object(teatree_overlay_loader, "get_all_overlays", return_value={"test": overlay_stub}),
            patch.object(teatree_cli_doctor, "packages_distributions", return_value={"my_overlay": ["my-overlay"]}),
            patch("teatree.config.load_config", return_value=mock_config),
        ):
            result = DoctorService.check_editable_sanity()
            assert any("contribute=false" in p for p in result)

    def test_no_warnings_when_editable_state_matches(self):
        """No warnings when contribute=false and nothing is editable."""
        mock_config = MagicMock()
        mock_config.user.contribute = False

        overlay_stub = _make_overlay_stub("my_overlay.overlay")

        with (
            patch.object(IntrospectionHelpers, "editable_info", return_value=(False, "")),
            patch.object(teatree_overlay_loader, "get_all_overlays", return_value={"test": overlay_stub}),
            patch.object(teatree_cli_doctor, "packages_distributions", return_value={"my_overlay": ["my-overlay"]}),
            patch("teatree.config.load_config", return_value=mock_config),
        ):
            result = DoctorService.check_editable_sanity()
            assert result == []

    def test_warns_overlay_contribute_true_repo_not_found(self):
        """Warns when contribute=true, overlay not editable, and repo not found."""
        mock_config = MagicMock()
        mock_config.user.contribute = True

        overlay_stub = _make_overlay_stub("my_overlay.overlay")

        def editable_info(dist_name):
            return (dist_name == "teatree", "")  # teatree editable, overlay not

        with (
            patch.object(IntrospectionHelpers, "editable_info", side_effect=editable_info),
            patch.object(teatree_overlay_loader, "get_all_overlays", return_value={"test": overlay_stub}),
            patch.object(teatree_cli_doctor, "packages_distributions", return_value={"my_overlay": ["my-overlay"]}),
            patch("teatree.config.load_config", return_value=mock_config),
            patch.object(DoctorService, "find_overlay_repo", return_value=None),
        ):
            result = DoctorService.check_editable_sanity()
            assert any("overlay" in p and "repo not found" in p for p in result)

    # ── find_teatree_repo ───────────────────────────────────────────

    def test_find_teatree_repo_from_env(self, tmp_path, monkeypatch):
        """Finds teatree repo via T3_REPO env var."""
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'teatree'\n")
        monkeypatch.setenv("T3_REPO", str(tmp_path))
        assert DoctorService.find_teatree_repo() == tmp_path

    def test_find_teatree_repo_auto_detect(self, tmp_path, monkeypatch):
        """Auto-detects teatree repo via find_project_root."""
        monkeypatch.delenv("T3_REPO", raising=False)
        with patch("teatree.find_project_root", return_value=tmp_path):
            assert DoctorService.find_teatree_repo() == tmp_path

    def test_find_teatree_repo_returns_none(self, tmp_path, monkeypatch):
        """Returns None when T3_REPO not set and auto-detect fails."""
        monkeypatch.delenv("T3_REPO", raising=False)
        with patch("teatree.find_project_root", return_value=None):
            assert DoctorService.find_teatree_repo() is None

    # ── find_overlay_repo ───────────────────────────────────────────

    def test_find_overlay_repo_found(self, tmp_path):
        """Finds overlay repo in workspace directory."""
        overlay_dir = tmp_path / "my-overlay"
        overlay_dir.mkdir()
        (overlay_dir / "pyproject.toml").write_text("[project]\nname = 'my-overlay'\n")

        mock_config = MagicMock()
        mock_config.user.workspace_dir = str(tmp_path)
        with patch("teatree.config.load_config", return_value=mock_config):
            assert DoctorService.find_overlay_repo("my-overlay") == overlay_dir

    def test_find_overlay_repo_not_found(self, tmp_path):
        """Returns None when overlay repo not in workspace."""
        mock_config = MagicMock()
        mock_config.user.workspace_dir = str(tmp_path)
        with patch("teatree.config.load_config", return_value=mock_config):
            assert DoctorService.find_overlay_repo("nonexistent") is None

    # ── make_editable ───────────────────────────────────────────────

    def test_make_editable_success(self, capsys, tmp_path):
        """Patches pyproject.toml sources and reports success."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[tool.uv.sources]\nteatree = { git = "https://example.com", branch = "main" }\n')
        (tmp_path / "manage.py").write_text("")  # host project marker

        mock_result = MagicMock()
        mock_result.returncode = 0
        with (
            patch("teatree.cli.doctor._find_host_project_root", return_value=tmp_path),
            patch("subprocess.run", return_value=mock_result),
        ):
            DoctorService.make_editable("teatree", Path("/tmp/teatree"))
        captured = capsys.readouterr()
        assert "now editable" in captured.out
        assert (tmp_path / ".t3-dev-sources").is_file()

    def test_make_editable_no_host_project(self, capsys):
        """Falls back to ephemeral uv pip install when no host project found."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        with (
            patch("teatree.cli.doctor._find_host_project_root", return_value=None),
            patch("subprocess.run", return_value=mock_result),
        ):
            DoctorService.make_editable("teatree", Path("/tmp/teatree"))
        captured = capsys.readouterr()
        assert "ephemeral" in captured.out

    def test_make_editable_patch_failure(self, capsys, tmp_path):
        """Reports failure when pyproject.toml has no matching source entry."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text("[project]\nname = 'myproject'\n")
        (tmp_path / "manage.py").write_text("")

        with patch("teatree.cli.doctor._find_host_project_root", return_value=tmp_path):
            DoctorService.make_editable("teatree", Path("/tmp/teatree"))
        captured = capsys.readouterr()
        assert "FAIL" in captured.out


class TestIntrospectionHelpers:
    """Tests for IntrospectionHelpers methods (print_package_info, editable_info)."""

    # ── editable_info ────────────────────────────────────────────────

    def test_not_installed(self):
        with patch.object(teatree_cli_doctor, "distribution", side_effect=teatree_cli_doctor.PackageNotFoundError("x")):
            assert IntrospectionHelpers.editable_info("nonexistent") == (False, "")

    def test_no_direct_url(self):
        mock_dist = MagicMock()
        mock_dist.read_text.return_value = None
        with patch.object(teatree_cli_doctor, "distribution", return_value=mock_dist):
            assert IntrospectionHelpers.editable_info("some-pkg") == (False, "")

    def test_editable(self):
        mock_dist = MagicMock()
        mock_dist.read_text.return_value = json.dumps(
            {
                "dir_info": {"editable": True},
                "url": "file:///home/user/project",
            }
        )
        with patch.object(teatree_cli_doctor, "distribution", return_value=mock_dist):
            editable, url = IntrospectionHelpers.editable_info("some-pkg")
            assert editable is True
            assert url == "file:///home/user/project"

    def test_invalid_json(self):
        mock_dist = MagicMock()
        mock_dist.read_text.return_value = "not json"
        with patch.object(teatree_cli_doctor, "distribution", return_value=mock_dist):
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

    def test_check_validates_skills(self, tmp_path, monkeypatch):
        """Doctor check validates SKILL.md files in skills directory."""
        claude_skills = tmp_path / ".claude" / "skills"
        ok = claude_skills / "ok-skill"
        ok.mkdir(parents=True)
        (ok / "SKILL.md").write_text("---\nname: ok-skill\ndescription: d\n---\n")

        monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: tmp_path))
        with patch.object(DoctorService, "check_editable_sanity", return_value=[]):
            result = runner.invoke(app, ["doctor", "check"])
            assert result.exit_code == 0
            assert "1 skill(s) validated" in result.output

    def test_check_skill_validation_errors(self, tmp_path, monkeypatch):
        """Doctor check reports skill validation errors."""
        claude_skills = tmp_path / ".claude" / "skills"
        bad = claude_skills / "bad-skill"
        bad.mkdir(parents=True)
        (bad / "SKILL.md").write_text("no frontmatter here")

        monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: tmp_path))
        with patch.object(DoctorService, "check_editable_sanity", return_value=[]):
            result = runner.invoke(app, ["doctor", "check"])
            assert "FAIL" in result.output

    def test_check_skill_validation_warnings(self, tmp_path, monkeypatch):
        """Doctor check reports skill validation warnings for unknown fields."""
        claude_skills = tmp_path / ".claude" / "skills"
        skill = claude_skills / "warn-skill"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("---\nname: warn-skill\ndescription: d\nunknown-field: x\n---\n")

        monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: tmp_path))
        with patch.object(DoctorService, "check_editable_sanity", return_value=[]):
            result = runner.invoke(app, ["doctor", "check"])
            assert "WARN" in result.output

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


class TestFindHostProjectRoot:
    def test_finds_project_in_current_dir(self, tmp_path: Path) -> None:
        (tmp_path / "manage.py").write_text("", encoding="utf-8")
        (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
        with patch("teatree.cli.doctor.Path") as mock_path:
            mock_path.cwd.return_value = tmp_path
            result = teatree_cli_doctor._find_host_project_root()
        assert result == tmp_path

    def test_returns_none_when_not_found(self, tmp_path: Path) -> None:
        with patch("teatree.cli.doctor.Path") as mock_path:
            mock_path.cwd.return_value = tmp_path
            result = teatree_cli_doctor._find_host_project_root()
        assert result is None


class TestWriteDevSourcesMarker:
    def test_creates_new_marker(self, tmp_path: Path) -> None:
        marker = tmp_path / ".t3-dev-sources"
        teatree_cli_doctor._write_dev_sources_marker(marker, "teatree", Path("/repos/teatree"))
        content = marker.read_text(encoding="utf-8")
        assert "teatree=/repos/teatree" in content

    def test_updates_existing_entry(self, tmp_path: Path) -> None:
        marker = tmp_path / ".t3-dev-sources"
        marker.write_text("teatree=/old/path\nother=/other/path\n", encoding="utf-8")
        teatree_cli_doctor._write_dev_sources_marker(marker, "teatree", Path("/new/path"))
        content = marker.read_text(encoding="utf-8")
        assert "teatree=/new/path" in content
        assert "other=/other/path" in content
        assert "/old/path" not in content


class TestRestoreSources:
    def test_restores_from_marker(self, tmp_path: Path) -> None:
        marker = tmp_path / ".t3-dev-sources"
        marker.write_text("teatree=/repos/teatree\n", encoding="utf-8")
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text("[project]\nname = 'test'\n", encoding="utf-8")

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 0)
            DoctorService.restore_sources(tmp_path)
        assert not marker.exists()
        assert mock_run.call_count == 2  # git update-index + git checkout

    def test_noop_when_no_marker(self, tmp_path: Path) -> None:
        # No marker file — nothing should happen
        DoctorService.restore_sources(tmp_path)


class TestMakeEditableFailure:
    def test_reports_failure_without_host_project(self, tmp_path: Path) -> None:
        with (
            patch("teatree.cli.doctor._find_host_project_root", return_value=None),
            patch(
                "subprocess.run",
                return_value=subprocess.CompletedProcess([], 1, "", "install failed"),
            ),
        ):
            DoctorService.make_editable("teatree", tmp_path)

    def test_reports_failure_on_uv_sync(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "test"\n\n[tool.uv.sources]\nteatree = { git = "https://x" }\n',
            encoding="utf-8",
        )
        with (
            patch("teatree.cli.doctor._find_host_project_root", return_value=tmp_path),
            patch(
                "subprocess.run",
                return_value=subprocess.CompletedProcess([], 1, "", "sync failed"),
            ),
        ):
            DoctorService.make_editable("teatree", Path("/repos/teatree"))
