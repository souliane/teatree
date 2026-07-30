"""``DoctorService.show_info`` — environment + overlay state pretty-printer.

Lifted verbatim from the former monolithic ``tests/test_cli_doctor.py``
(souliane/teatree#443). No behavior change: same assertions and helpers,
only relocated under a focused package by concern.

Integration-first per the Test-Writing Doctrine: real DB-home ``overlays``
registry seeds (legacy file tier removed) and real ``~/.claude/plugins``
filesystem layouts; only the unstoppable externals (``shutil.which``,
``editable_info``, ``print_package_info``) stay mocked.
"""

import json
from unittest.mock import patch

from teatree.cli.doctor import DoctorService, IntrospectionHelpers

from ._shared import _seed_overlays, _stage_home


class TestShowInfo:
    """``DoctorService.show_info`` pretty-prints environment + overlay state."""

    def test_prints_active_overlay_from_registry(self, tmp_path, monkeypatch, capsys):
        _stage_home(tmp_path, monkeypatch)
        _seed_overlays(tmp_path, monkeypatch, {"acme": {"class": "acme.overlay:AcmeOverlay"}})
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
        _seed_overlays(tmp_path, monkeypatch, {"acme": {"path": str(project)}})

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
        _seed_overlays(tmp_path, monkeypatch, {"acme": {"class": "acme.overlay:AcmeOverlay"}})

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
