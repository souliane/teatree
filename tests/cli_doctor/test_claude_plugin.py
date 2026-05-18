"""``DoctorService.find_installed_claude_plugin`` — installed-plugin lookup.

Lifted verbatim from the former monolithic ``tests/test_cli_doctor.py``
(souliane/teatree#443). No behavior change: same assertions and helpers,
only relocated under a focused package by concern.
"""

import json

from teatree.cli.doctor import DoctorService

from ._shared import _stage_home


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

    def test_detects_symlink_install(self, tmp_path, monkeypatch):
        _stage_home(tmp_path, monkeypatch)
        plugins_dir = tmp_path / ".claude" / "plugins"
        plugins_dir.mkdir(parents=True)
        target = tmp_path / "teatree-clone"
        target.mkdir()
        (plugins_dir / "t3").symlink_to(target)
        result = DoctorService.find_installed_claude_plugin()
        assert result is not None
        assert result["scope"] == "symlink"
        assert result["installPath"] == str(target)

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
