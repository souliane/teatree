"""Tests for doctor-related CLI commands.

Integration-first per the Test-Writing Doctrine: real ``~/.teatree.toml``
fixtures, real ``~/.claude/plugins/installed_plugins.json`` files, and real
filesystem layouts replace patches of teatree-internal helpers
(``discover_overlays``, ``load_config``, ``find_teatree_repo``,
``find_overlay_repo``, ``find_installed_claude_plugin``, …).

Remaining mocks cover unstoppable externals only:

- ``importlib.metadata.distribution`` / ``packages_distributions`` (installed-package
    introspection — would otherwise require fixture packages actually installed into
    the test venv).
- ``importlib.import_module`` (used by ``print_package_info``).
- ``shutil.which`` (PATH lookup for ``t3`` / ``direnv`` / ``git`` / ``jq``).
- ``subprocess.run`` (``uv pip install``, ``git update-index`` …).
"""

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

import teatree.cli.doctor as teatree_cli_doctor
import teatree.core.overlay_loader as teatree_overlay_loader
from teatree.cli import app
from teatree.cli.doctor import DoctorService, IntrospectionHelpers

runner = CliRunner()


# ── Helpers ──────────────────────────────────────────────────────────────


def _write_teatree_toml(config_path: Path, content: str) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(content, encoding="utf-8")


def _stage_home(tmp_path: Path, monkeypatch) -> Path:
    """Isolate overlay discovery under ``tmp_path``.

    - Redirects ``Path.home()`` to ``tmp_path`` so ``~/.claude/...`` lookups are sandboxed.
    - Redirects ``teatree.config.CONFIG_PATH`` to ``tmp_path/.teatree.toml``.
    - Muzzles ``importlib.metadata.entry_points`` so installed overlays (``t3-teatree``)
        don't leak into ``discover_overlays()`` / ``discover_active_overlay()``.
    - Moves cwd under ``tmp_path`` so ``_discover_from_manage_py`` cannot climb into
        the real teatree checkout.
    """
    monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr("teatree.config.CONFIG_PATH", tmp_path / ".teatree.toml")
    monkeypatch.setattr("importlib.metadata.entry_points", lambda **_kw: [])
    neutral = tmp_path / "_neutral_cwd"
    neutral.mkdir(exist_ok=True)
    monkeypatch.chdir(neutral)
    monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)
    return tmp_path


def _stub_overlay_instance(module: str = "my_overlay.overlay") -> object:
    """Return an instance whose ``type(inst).__module__`` is *module*.

    ``_resolve_overlay_dists`` inspects the instance's class module, not the
    instance module. A plain ``MagicMock`` would report ``unittest.mock``.
    """
    cls = type("_OverlayStub", (), {"__module__": module})
    return cls()


def _editable_map(**dists: tuple[bool, str]):
    """Build an ``editable_info`` side_effect from a ``dist_name -> (editable, url)`` map."""

    def side_effect(dist_name: str) -> tuple[bool, str]:
        return dists.get(dist_name, (False, ""))

    return side_effect


# ── DoctorService.show_info ─────────────────────────────────────────────


class TestShowInfo:
    """``DoctorService.show_info`` pretty-prints environment + overlay state."""

    def test_prints_active_overlay_from_toml(self, tmp_path, monkeypatch, capsys):
        _stage_home(tmp_path, monkeypatch)
        _write_teatree_toml(
            tmp_path / ".teatree.toml",
            '[overlays.acme]\nclass = "acme.overlay:AcmeOverlay"\n',
        )
        monkeypatch.setenv("T3_OVERLAY_NAME", "acme")

        with (
            patch("shutil.which", return_value="/usr/bin/t3"),
            patch.object(IntrospectionHelpers, "editable_info", return_value=(False, "")),
            patch.object(IntrospectionHelpers, "print_package_info"),
        ):
            DoctorService.show_info()

        out = capsys.readouterr().out
        assert "Active overlay:" in out
        assert "acme" in out

    def test_prints_no_overlay_when_toml_missing(self, tmp_path, monkeypatch, capsys):
        _stage_home(tmp_path, monkeypatch)

        with (
            patch("shutil.which", return_value=None),
            patch.object(IntrospectionHelpers, "editable_info", return_value=(False, "")),
            patch.object(IntrospectionHelpers, "print_package_info"),
        ):
            DoctorService.show_info()

        out = capsys.readouterr().out
        assert "Active overlay:   (none)" in out

    def test_prints_overlay_project_path_when_set(self, tmp_path, monkeypatch, capsys):
        _stage_home(tmp_path, monkeypatch)
        project = tmp_path / "my-overlay"
        project.mkdir()
        (project / "manage.py").write_text('os.environ.setdefault("DJANGO_SETTINGS_MODULE", "myproj.settings")\n')
        _write_teatree_toml(
            tmp_path / ".teatree.toml",
            f'[overlays.acme]\npath = "{project}"\n',
        )

        with (
            patch("shutil.which", return_value="/usr/bin/t3"),
            patch.object(IntrospectionHelpers, "editable_info", return_value=(False, "")),
            patch.object(IntrospectionHelpers, "print_package_info"),
        ):
            DoctorService.show_info()

        out = capsys.readouterr().out
        assert "acme" in out
        assert str(project) in out

    def test_omits_project_path_row_when_none(self, tmp_path, monkeypatch, capsys):
        """When an overlay has no ``project_path``, no second indented path row is printed."""
        _stage_home(tmp_path, monkeypatch)
        _write_teatree_toml(
            tmp_path / ".teatree.toml",
            '[overlays.acme]\nclass = "acme.overlay:AcmeOverlay"\n',
        )

        with (
            patch("shutil.which", return_value="/usr/bin/t3"),
            patch.object(IntrospectionHelpers, "editable_info", return_value=(False, "")),
            patch.object(IntrospectionHelpers, "print_package_info"),
        ):
            DoctorService.show_info()

        lines = capsys.readouterr().out.splitlines()
        # The "Installed overlays:" block lists ``acme`` once but must NOT emit a
        # trailing indented path row for the TOML entry (which has no ``path``).
        installed_idx = next(i for i, line in enumerate(lines) if line.startswith("Installed overlays:"))
        overlay_block = lines[installed_idx + 1 : installed_idx + 3]
        assert any("acme" in line for line in overlay_block)
        assert all(str(tmp_path) not in line for line in overlay_block)

    def test_shows_claude_plugin_when_installed(self, tmp_path, monkeypatch, capsys):
        _stage_home(tmp_path, monkeypatch)
        plugins_file = tmp_path / ".claude" / "plugins" / "installed_plugins.json"
        plugins_file.parent.mkdir(parents=True)
        plugins_file.write_text(
            json.dumps(
                {
                    "version": 2,
                    "plugins": {
                        "t3@souliane": [
                            {
                                "scope": "user",
                                "installPath": "/Users/x/.claude/plugins/cache/souliane/t3/0.0.1",
                                "version": "0.0.1",
                            },
                        ],
                    },
                },
            ),
        )

        with (
            patch("shutil.which", return_value="/usr/bin/t3"),
            patch.object(IntrospectionHelpers, "editable_info", return_value=(False, "")),
            patch.object(IntrospectionHelpers, "print_package_info"),
        ):
            DoctorService.show_info()

        out = capsys.readouterr().out
        assert "Claude plugin:" in out
        assert "0.0.1" in out
        assert "user" in out
        assert "/Users/x/.claude/plugins/cache/souliane/t3/0.0.1" in out

    def test_says_plugin_not_installed_when_missing(self, tmp_path, monkeypatch, capsys):
        _stage_home(tmp_path, monkeypatch)

        with (
            patch("shutil.which", return_value="/usr/bin/t3"),
            patch.object(IntrospectionHelpers, "editable_info", return_value=(False, "")),
            patch.object(IntrospectionHelpers, "print_package_info"),
        ):
            DoctorService.show_info()

        out = capsys.readouterr().out
        assert "Claude plugin:" in out
        assert "not installed" in out

    def test_lists_existing_runtime_skill_dirs(self, tmp_path, monkeypatch, capsys):
        _stage_home(tmp_path, monkeypatch)
        (tmp_path / ".claude" / "skills").mkdir(parents=True)
        (tmp_path / ".codex" / "skills").mkdir(parents=True)
        fake_target = tmp_path / "source" / "code"
        fake_target.mkdir(parents=True)
        (tmp_path / ".claude" / "skills" / "code").symlink_to(fake_target)
        (tmp_path / ".codex" / "skills" / "code").symlink_to(fake_target)

        with (
            patch("shutil.which", return_value="/usr/bin/t3"),
            patch.object(IntrospectionHelpers, "editable_info", return_value=(False, "")),
            patch.object(IntrospectionHelpers, "print_package_info"),
        ):
            DoctorService.show_info()

        out = capsys.readouterr().out
        assert "Skills installed to:" in out
        assert str(tmp_path / ".claude" / "skills") in out
        assert str(tmp_path / ".codex" / "skills") in out

    def test_skips_missing_runtime_skill_dirs(self, tmp_path, monkeypatch, capsys):
        _stage_home(tmp_path, monkeypatch)
        (tmp_path / ".claude" / "skills").mkdir(parents=True)
        # No ~/.codex.

        with (
            patch("shutil.which", return_value="/usr/bin/t3"),
            patch.object(IntrospectionHelpers, "editable_info", return_value=(False, "")),
            patch.object(IntrospectionHelpers, "print_package_info"),
        ):
            DoctorService.show_info()

        out = capsys.readouterr().out
        assert str(tmp_path / ".claude" / "skills") in out
        assert ".codex" not in out


# ── DoctorService.find_installed_claude_plugin ───────────────────────────


class TestFindInstalledClaudePlugin:
    def test_returns_entry_when_installed(self, tmp_path, monkeypatch):
        _stage_home(tmp_path, monkeypatch)
        plugins_file = tmp_path / ".claude" / "plugins" / "installed_plugins.json"
        plugins_file.parent.mkdir(parents=True)
        plugins_file.write_text(
            json.dumps(
                {
                    "version": 2,
                    "plugins": {
                        "t3@souliane": [
                            {
                                "scope": "user",
                                "installPath": "/path/to/t3/0.0.1",
                                "version": "0.0.1",
                            },
                        ],
                    },
                },
            ),
        )
        assert DoctorService.find_installed_claude_plugin() == {
            "version": "0.0.1",
            "installPath": "/path/to/t3/0.0.1",
            "scope": "user",
        }

    def test_returns_none_when_file_missing(self, tmp_path, monkeypatch):
        _stage_home(tmp_path, monkeypatch)
        assert DoctorService.find_installed_claude_plugin() is None

    def test_returns_none_when_entry_missing(self, tmp_path, monkeypatch):
        _stage_home(tmp_path, monkeypatch)
        plugins_file = tmp_path / ".claude" / "plugins" / "installed_plugins.json"
        plugins_file.parent.mkdir(parents=True)
        plugins_file.write_text(json.dumps({"version": 2, "plugins": {}}))
        assert DoctorService.find_installed_claude_plugin() is None

    def test_returns_none_when_malformed_json(self, tmp_path, monkeypatch):
        _stage_home(tmp_path, monkeypatch)
        plugins_file = tmp_path / ".claude" / "plugins" / "installed_plugins.json"
        plugins_file.parent.mkdir(parents=True)
        plugins_file.write_text("not json")
        assert DoctorService.find_installed_claude_plugin() is None


# ── DoctorService.collect_overlay_skills ─────────────────────────────────


class TestCollectOverlaySkills:
    def test_returns_skills_from_skills_subdir(self, tmp_path, monkeypatch):
        _stage_home(tmp_path, monkeypatch)
        project = tmp_path / "my-project"
        skill = project / "skills" / "custom"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").touch()
        _write_teatree_toml(
            tmp_path / ".teatree.toml",
            f'[overlays.my-overlay]\npath = "{project}"\n',
        )

        results = DoctorService.collect_overlay_skills()

        assert (skill, "custom") in results

    def test_ignores_legacy_subdir_convention(self, tmp_path, monkeypatch):
        """Overlay skills require a ``skills/`` dir; loose SKILL.md siblings don't count."""
        _stage_home(tmp_path, monkeypatch)
        project = tmp_path / "my-overlay"
        project.mkdir()
        overlay_subdir = project / "my_app"
        overlay_subdir.mkdir()
        (overlay_subdir / "SKILL.md").touch()
        _write_teatree_toml(
            tmp_path / ".teatree.toml",
            f'[overlays.my-overlay]\npath = "{project}"\n',
        )

        results = DoctorService.collect_overlay_skills()

        assert all(name != "my_app" for _, name in results)

    def test_skips_entries_without_project_path(self, tmp_path, monkeypatch):
        _stage_home(tmp_path, monkeypatch)
        _write_teatree_toml(
            tmp_path / ".teatree.toml",
            '[overlays.classonly]\nclass = "acme.overlay:AcmeOverlay"\n',
        )

        results = DoctorService.collect_overlay_skills()

        assert all(name != "classonly" for _, name in results)


# ── DoctorService.repair_symlinks ────────────────────────────────────────


class TestRepairSymlinks:
    def test_creates_missing_link(self, tmp_path, monkeypatch):
        _stage_home(tmp_path, monkeypatch)
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        (skills_dir / "code").mkdir()
        (skills_dir / "code" / "SKILL.md").touch()
        claude_skills = tmp_path / "claude_skills"
        claude_skills.mkdir()

        created, fixed = DoctorService.repair_symlinks(skills_dir, claude_skills)

        assert (created, fixed) == (1, 0)
        assert (claude_skills / "code").is_symlink()

    def test_handles_empty_skills_dir(self, tmp_path, monkeypatch):
        _stage_home(tmp_path, monkeypatch)
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        (skills_dir / "not-a-skill").mkdir()  # No SKILL.md.
        claude_skills = tmp_path / "claude_skills"
        claude_skills.mkdir()

        created, fixed = DoctorService.repair_symlinks(skills_dir, claude_skills)

        assert (created, fixed) == (0, 0)

    def test_fixes_wrong_target(self, tmp_path, monkeypatch):
        _stage_home(tmp_path, monkeypatch)
        skills_dir = tmp_path / "skills"
        skill = skills_dir / "code"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").touch()
        claude_skills = tmp_path / "claude_skills"
        claude_skills.mkdir()
        wrong_target = tmp_path / "wrong"
        wrong_target.mkdir()
        (claude_skills / "code").symlink_to(wrong_target)

        created, fixed = DoctorService.repair_symlinks(skills_dir, claude_skills)

        assert (created, fixed) == (1, 1)

    def test_skips_real_directory(self, tmp_path, monkeypatch):
        _stage_home(tmp_path, monkeypatch)
        skills_dir = tmp_path / "skills"
        skill = skills_dir / "code"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").touch()
        claude_skills = tmp_path / "claude_skills"
        claude_skills.mkdir()
        (claude_skills / "code").mkdir()

        created, fixed = DoctorService.repair_symlinks(skills_dir, claude_skills)

        assert (created, fixed) == (0, 0)

    def test_leaves_correct_link_unchanged(self, tmp_path, monkeypatch):
        _stage_home(tmp_path, monkeypatch)
        skills_dir = tmp_path / "skills"
        skill = skills_dir / "code"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").touch()
        claude_skills = tmp_path / "claude_skills"
        claude_skills.mkdir()
        (claude_skills / "code").symlink_to(skill)

        created, fixed = DoctorService.repair_symlinks(skills_dir, claude_skills)

        assert (created, fixed) == (0, 0)


# ── DoctorService.check_editable_sanity ──────────────────────────────────


class TestCheckEditableSanity:
    """End-to-end sanity check wired to a real ``~/.teatree.toml``.

    ``editable_info`` and ``get_all_overlays`` are the two external boundaries
    we cannot make real without installing actual packages, so they stay as
    mocks. Everything else (config loading, repo discovery) runs live.
    """

    def test_empty_when_contribute_false_and_nothing_editable(self, tmp_path, monkeypatch):
        _stage_home(tmp_path, monkeypatch)
        _write_teatree_toml(tmp_path / ".teatree.toml", "[teatree]\ncontribute = false\n")

        with (
            patch.object(IntrospectionHelpers, "editable_info", return_value=(False, "")),
            patch.object(teatree_overlay_loader, "get_all_overlays", return_value={}),
        ):
            assert DoctorService.check_editable_sanity() == []

    def test_empty_when_contribute_true_and_all_editable(self, tmp_path, monkeypatch):
        _stage_home(tmp_path, monkeypatch)
        _write_teatree_toml(tmp_path / ".teatree.toml", "[teatree]\ncontribute = true\n")

        with (
            patch.object(IntrospectionHelpers, "editable_info", return_value=(True, "file:///src")),
            patch.object(teatree_overlay_loader, "get_all_overlays", return_value={}),
        ):
            assert DoctorService.check_editable_sanity() == []

    def test_auto_fixes_teatree_when_contribute_true(self, tmp_path, monkeypatch):
        _stage_home(tmp_path, monkeypatch)
        _write_teatree_toml(tmp_path / ".teatree.toml", "[teatree]\ncontribute = true\n")
        teatree_repo = tmp_path / "repos" / "teatree"
        teatree_repo.mkdir(parents=True)
        (teatree_repo / "pyproject.toml").write_text('[project]\nname = "teatree"\n')
        monkeypatch.setenv("T3_REPO", str(teatree_repo))
        monkeypatch.chdir(tmp_path)

        with (
            patch.object(IntrospectionHelpers, "editable_info", return_value=(False, "")),
            patch.object(teatree_overlay_loader, "get_all_overlays", return_value={}),
            patch.object(DoctorService, "make_editable") as mock_fix,
        ):
            problems = DoctorService.check_editable_sanity()

        mock_fix.assert_called_once_with("teatree", teatree_repo)
        assert problems == []

    def test_warns_when_contribute_true_and_teatree_repo_not_found(self, tmp_path, monkeypatch):
        _stage_home(tmp_path, monkeypatch)
        _write_teatree_toml(tmp_path / ".teatree.toml", "[teatree]\ncontribute = true\n")
        monkeypatch.delenv("T3_REPO", raising=False)
        monkeypatch.chdir(tmp_path)

        with (
            patch.object(IntrospectionHelpers, "editable_info", return_value=(False, "")),
            patch.object(teatree_overlay_loader, "get_all_overlays", return_value={}),
            patch("teatree.find_project_root", return_value=None),
        ):
            problems = DoctorService.check_editable_sanity()

        assert any("contribute=true" in p for p in problems)

    def test_warns_when_teatree_editable_but_contribute_false(self, tmp_path, monkeypatch):
        _stage_home(tmp_path, monkeypatch)
        _write_teatree_toml(tmp_path / ".teatree.toml", "[teatree]\ncontribute = false\n")

        with (
            patch.object(IntrospectionHelpers, "editable_info", return_value=(True, "file:///src")),
            patch.object(teatree_overlay_loader, "get_all_overlays", return_value={}),
        ):
            problems = DoctorService.check_editable_sanity()

        assert any("contribute=false" in p for p in problems)

    def test_auto_fixes_overlay_when_contribute_true(self, tmp_path, monkeypatch):
        _stage_home(tmp_path, monkeypatch)
        _write_teatree_toml(
            tmp_path / ".teatree.toml",
            f'[teatree]\ncontribute = true\nworkspace_dir = "{tmp_path}"\n',
        )
        overlay_repo = tmp_path / "my-overlay"
        overlay_repo.mkdir()
        (overlay_repo / "pyproject.toml").write_text('[project]\nname = "my-overlay"\n')

        with (
            patch.object(
                IntrospectionHelpers,
                "editable_info",
                side_effect=_editable_map(teatree=(True, ""), **{"my-overlay": (False, "")}),
            ),
            patch.object(
                teatree_overlay_loader,
                "get_all_overlays",
                return_value={"test": _stub_overlay_instance()},
            ),
            patch.object(
                teatree_cli_doctor,
                "packages_distributions",
                return_value={"my_overlay": ["my-overlay"]},
            ),
            patch.object(DoctorService, "make_editable") as mock_fix,
        ):
            problems = DoctorService.check_editable_sanity()

        mock_fix.assert_called_once_with("my-overlay", overlay_repo)
        assert problems == []

    def test_warns_when_overlay_editable_but_contribute_false(self, tmp_path, monkeypatch):
        _stage_home(tmp_path, monkeypatch)
        _write_teatree_toml(tmp_path / ".teatree.toml", "[teatree]\ncontribute = false\n")

        with (
            patch.object(
                IntrospectionHelpers,
                "editable_info",
                side_effect=_editable_map(teatree=(False, ""), **{"my-overlay": (True, "file:///src")}),
            ),
            patch.object(
                teatree_overlay_loader,
                "get_all_overlays",
                return_value={"test": _stub_overlay_instance()},
            ),
            patch.object(
                teatree_cli_doctor,
                "packages_distributions",
                return_value={"my_overlay": ["my-overlay"]},
            ),
        ):
            problems = DoctorService.check_editable_sanity()

        assert any("contribute=false" in p for p in problems)

    def test_empty_when_all_states_align_with_contribute_false(self, tmp_path, monkeypatch):
        _stage_home(tmp_path, monkeypatch)
        _write_teatree_toml(tmp_path / ".teatree.toml", "[teatree]\ncontribute = false\n")

        with (
            patch.object(IntrospectionHelpers, "editable_info", return_value=(False, "")),
            patch.object(
                teatree_overlay_loader,
                "get_all_overlays",
                return_value={"test": _stub_overlay_instance()},
            ),
            patch.object(
                teatree_cli_doctor,
                "packages_distributions",
                return_value={"my_overlay": ["my-overlay"]},
            ),
        ):
            assert DoctorService.check_editable_sanity() == []

    def test_warns_when_overlay_repo_not_found(self, tmp_path, monkeypatch):
        _stage_home(tmp_path, monkeypatch)
        _write_teatree_toml(
            tmp_path / ".teatree.toml",
            f'[teatree]\ncontribute = true\nworkspace_dir = "{tmp_path}"\n',
        )
        # No ``my-overlay`` directory under workspace_dir.

        with (
            patch.object(
                IntrospectionHelpers,
                "editable_info",
                side_effect=_editable_map(teatree=(True, ""), **{"my-overlay": (False, "")}),
            ),
            patch.object(
                teatree_overlay_loader,
                "get_all_overlays",
                return_value={"test": _stub_overlay_instance()},
            ),
            patch.object(
                teatree_cli_doctor,
                "packages_distributions",
                return_value={"my_overlay": ["my-overlay"]},
            ),
        ):
            problems = DoctorService.check_editable_sanity()

        assert any("overlay" in p and "repo not found" in p for p in problems)


# ── DoctorService.find_teatree_repo ──────────────────────────────────────


class TestFindTeatreeRepo:
    def test_finds_via_t3_repo_env(self, tmp_path, monkeypatch):
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "teatree"\n')
        monkeypatch.setenv("T3_REPO", str(tmp_path))
        monkeypatch.chdir(tmp_path.parent)

        assert DoctorService.find_teatree_repo() == tmp_path

    def test_auto_detects_via_find_project_root(self, tmp_path, monkeypatch):
        monkeypatch.delenv("T3_REPO", raising=False)
        monkeypatch.chdir(tmp_path.parent)

        with patch("teatree.find_project_root", return_value=tmp_path):
            assert DoctorService.find_teatree_repo() == tmp_path

    def test_returns_none_when_env_missing_and_auto_detect_fails(self, tmp_path, monkeypatch):
        monkeypatch.delenv("T3_REPO", raising=False)
        monkeypatch.chdir(tmp_path)

        with patch("teatree.find_project_root", return_value=None):
            assert DoctorService.find_teatree_repo() is None

    def test_prefers_cwd_worktree_over_t3_repo_env(self, tmp_path, monkeypatch):
        main_clone = tmp_path / "main"
        main_clone.mkdir()
        (main_clone / "pyproject.toml").write_text('[project]\nname = "teatree"\n')
        worktree = tmp_path / "ac-123-ticket" / "teatree"
        worktree.mkdir(parents=True)
        (worktree / "pyproject.toml").write_text('[project]\nname = "teatree"\n')
        monkeypatch.setenv("T3_REPO", str(main_clone))
        monkeypatch.chdir(worktree)

        assert DoctorService.find_teatree_repo() == worktree


# ── DoctorService.find_overlay_repo ──────────────────────────────────────


class TestFindOverlayRepo:
    def test_finds_overlay_in_workspace(self, tmp_path, monkeypatch):
        _stage_home(tmp_path, monkeypatch)
        _write_teatree_toml(
            tmp_path / ".teatree.toml",
            f'[teatree]\nworkspace_dir = "{tmp_path}"\n',
        )
        overlay_dir = tmp_path / "my-overlay"
        overlay_dir.mkdir()
        (overlay_dir / "pyproject.toml").write_text('[project]\nname = "my-overlay"\n')

        assert DoctorService.find_overlay_repo("my-overlay") == overlay_dir

    def test_returns_none_when_overlay_absent(self, tmp_path, monkeypatch):
        _stage_home(tmp_path, monkeypatch)
        _write_teatree_toml(
            tmp_path / ".teatree.toml",
            f'[teatree]\nworkspace_dir = "{tmp_path}"\n',
        )

        assert DoctorService.find_overlay_repo("nonexistent") is None


# ── DoctorService.make_editable ──────────────────────────────────────────


class TestMakeEditable:
    """``make_editable`` shells out to ``uv``/``git``; those are the boundary mocks."""

    def test_success_patches_pyproject_and_writes_marker(self, tmp_path, capsys):
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[tool.uv.sources]\nteatree = { git = "https://example.com", branch = "main" }\n')
        (tmp_path / "manage.py").write_text("")

        success = subprocess.CompletedProcess([], 0)
        with (
            patch("teatree.cli.doctor._find_host_project_root", return_value=tmp_path),
            patch("subprocess.run", return_value=success),
        ):
            DoctorService.make_editable("teatree", Path("/tmp/teatree"))

        assert "now editable" in capsys.readouterr().out
        assert (tmp_path / ".t3-dev-sources").is_file()
        rewritten = pyproject.read_text()
        assert "path =" in rewritten
        assert "editable = true" in rewritten

    def test_falls_back_to_ephemeral_install_without_host_project(self, capsys):
        success = subprocess.CompletedProcess([], 0)
        with (
            patch("teatree.cli.doctor._find_host_project_root", return_value=None),
            patch("subprocess.run", return_value=success),
        ):
            DoctorService.make_editable("teatree", Path("/tmp/teatree"))

        assert "ephemeral" in capsys.readouterr().out

    def test_reports_fail_when_pyproject_has_no_source_entry(self, tmp_path, capsys):
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\nname = "myproject"\n')
        (tmp_path / "manage.py").write_text("")

        with patch("teatree.cli.doctor._find_host_project_root", return_value=tmp_path):
            DoctorService.make_editable("teatree", Path("/tmp/teatree"))

        assert "FAIL" in capsys.readouterr().out

    def test_reports_fail_without_host_project_when_install_fails(self, tmp_path):
        failure = subprocess.CompletedProcess([], 1, "", "install failed")
        with (
            patch("teatree.cli.doctor._find_host_project_root", return_value=None),
            patch("subprocess.run", return_value=failure),
        ):
            DoctorService.make_editable("teatree", tmp_path)

    def test_reports_fail_when_uv_sync_fails(self, tmp_path):
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "test"\n\n[tool.uv.sources]\nteatree = { git = "https://x" }\n',
        )
        failure = subprocess.CompletedProcess([], 1, "", "sync failed")
        with (
            patch("teatree.cli.doctor._find_host_project_root", return_value=tmp_path),
            patch("subprocess.run", return_value=failure),
        ):
            DoctorService.make_editable("teatree", Path("/repos/teatree"))


# ── IntrospectionHelpers ─────────────────────────────────────────────────


class TestEditableInfo:
    """``editable_info`` parses ``direct_url.json`` from an installed dist.

    ``importlib.metadata.distribution`` is the external boundary — it walks
    the real site-packages. We mock it rather than install fixture packages.
    """

    def test_returns_false_when_not_installed(self):
        with patch.object(
            teatree_cli_doctor,
            "distribution",
            side_effect=teatree_cli_doctor.PackageNotFoundError("x"),
        ):
            assert IntrospectionHelpers.editable_info("nonexistent") == (False, "")

    def test_returns_false_when_no_direct_url(self):
        dist = MagicMock()
        dist.read_text.return_value = None
        with patch.object(teatree_cli_doctor, "distribution", return_value=dist):
            assert IntrospectionHelpers.editable_info("some-pkg") == (False, "")

    def test_returns_editable_metadata_from_direct_url(self):
        dist = MagicMock()
        dist.read_text.return_value = json.dumps(
            {"dir_info": {"editable": True}, "url": "file:///home/user/project"},
        )
        with patch.object(teatree_cli_doctor, "distribution", return_value=dist):
            editable, url = IntrospectionHelpers.editable_info("some-pkg")

        assert editable is True
        assert url == "file:///home/user/project"

    def test_returns_false_on_invalid_direct_url_json(self):
        dist = MagicMock()
        dist.read_text.return_value = "not json"
        with patch.object(teatree_cli_doctor, "distribution", return_value=dist):
            assert IntrospectionHelpers.editable_info("some-pkg") == (False, "")


class TestPrintPackageInfo:
    """``print_package_info`` resolves ``import_name`` via ``importlib.import_module``."""

    def test_prints_source_dir_for_installed_package(self, capsys):
        with (
            patch("importlib.import_module") as mock_import,
            patch.object(IntrospectionHelpers, "editable_info", return_value=(False, "")),
        ):
            mod = MagicMock()
            mod.__file__ = "/usr/lib/python/teatree/__init__.py"
            mock_import.return_value = mod
            IntrospectionHelpers.print_package_info("teatree", "teatree")

        out = capsys.readouterr().out
        assert "/usr/lib/python/teatree" in out
        assert "installed" in out

    def test_handles_import_error(self, capsys):
        with patch("importlib.import_module", side_effect=ImportError("nope")):
            IntrospectionHelpers.print_package_info("teatree", "teatree")

        assert "not installed" in capsys.readouterr().out

    def test_prints_editable_url_when_available(self, capsys):
        with (
            patch("importlib.import_module") as mock_import,
            patch.object(IntrospectionHelpers, "editable_info", return_value=(True, "file:///src")),
        ):
            mod = MagicMock()
            mod.__file__ = "/src/teatree/__init__.py"
            mock_import.return_value = mod
            IntrospectionHelpers.print_package_info("teatree", "teatree")

        out = capsys.readouterr().out
        assert "editable" in out
        assert "file:///src" in out

    def test_omits_url_when_editable_but_none(self, capsys):
        with (
            patch("importlib.import_module") as mock_import,
            patch.object(IntrospectionHelpers, "editable_info", return_value=(True, "")),
        ):
            mod = MagicMock()
            mod.__file__ = "/src/teatree/__init__.py"
            mock_import.return_value = mod
            IntrospectionHelpers.print_package_info("teatree", "teatree")

        out = capsys.readouterr().out
        assert "editable" in out
        assert "file://" not in out


# ── t3 doctor check (CliRunner) ──────────────────────────────────────────


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


# ── Pure-function modules ────────────────────────────────────────────────


class TestFindHostProjectRoot:
    def test_finds_project_in_current_dir(self, tmp_path, monkeypatch):
        (tmp_path / "manage.py").write_text("")
        (tmp_path / "pyproject.toml").write_text("")
        monkeypatch.chdir(tmp_path)

        assert teatree_cli_doctor._find_host_project_root() == tmp_path

    def test_returns_none_when_not_found(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)

        assert teatree_cli_doctor._find_host_project_root() is None


class TestWriteDevSourcesMarker:
    def test_creates_new_marker_file(self, tmp_path):
        marker = tmp_path / ".t3-dev-sources"
        teatree_cli_doctor._write_dev_sources_marker(marker, "teatree", Path("/repos/teatree"))
        assert "teatree=/repos/teatree" in marker.read_text()

    def test_updates_existing_entry_in_place(self, tmp_path):
        marker = tmp_path / ".t3-dev-sources"
        marker.write_text("teatree=/old/path\nother=/other/path\n")
        teatree_cli_doctor._write_dev_sources_marker(marker, "teatree", Path("/new/path"))
        content = marker.read_text()
        assert "teatree=/new/path" in content
        assert "other=/other/path" in content
        assert "/old/path" not in content


class TestRestoreSources:
    def test_reverts_from_marker_via_git(self, tmp_path):
        marker = tmp_path / ".t3-dev-sources"
        marker.write_text("teatree=/repos/teatree\n")
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "test"\n')

        with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0)) as mock_run:
            DoctorService.restore_sources(tmp_path)

        assert not marker.exists()
        assert mock_run.call_count == 2  # git update-index + git checkout

    def test_noop_when_no_marker(self, tmp_path):
        DoctorService.restore_sources(tmp_path)  # must not raise
