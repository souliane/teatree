"""Tests for t3 setup Claude-plugin installation and apm-hook stripping.

Lifted verbatim from the former monolithic ``tests/test_cli_setup.py``
(souliane/teatree#443). No behavior change: same assertions, only
relocated under a focused package by concern.
"""

import json
from pathlib import Path

import pytest

from teatree.cli.setup.apm import strip_apm_hooks
from teatree.cli.setup.plugin_registrar import PluginRegistrar


class TestEnablePlugin:
    def test_adds_plugin_to_settings(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: tmp_path))
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        settings = claude_dir / "settings.json"
        settings.write_text(json.dumps({"key": "value"}))

        PluginRegistrar(tmp_path).enable()

        data = json.loads(settings.read_text())
        assert data["enabledPlugins"]["t3@souliane"] is True

    def test_noop_when_already_enabled(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: tmp_path))
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        settings = claude_dir / "settings.json"
        settings.write_text(json.dumps({"enabledPlugins": {"t3@souliane": True}}))
        mtime_before = settings.stat().st_mtime

        PluginRegistrar(tmp_path).enable()

        assert settings.stat().st_mtime == mtime_before


class TestRegisterInstalledPlugin:
    def test_registers_plugin_in_json(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: tmp_path))
        (tmp_path / ".claude" / "plugins").mkdir(parents=True)
        repo = tmp_path / "teatree-clone"
        repo.mkdir()

        PluginRegistrar(repo).register_installed()

        data = json.loads((tmp_path / ".claude" / "plugins" / "installed_plugins.json").read_text())
        entries = data["plugins"]["t3@souliane"]
        assert len(entries) == 1
        assert entries[0]["installPath"] == str(repo.resolve())
        assert entries[0]["version"] == "local"

    def test_noop_when_already_correct(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: tmp_path))
        plugins_dir = tmp_path / ".claude" / "plugins"
        plugins_dir.mkdir(parents=True)
        repo = tmp_path / "teatree-clone"
        repo.mkdir()

        PluginRegistrar(repo).register_installed()
        mtime = (plugins_dir / "installed_plugins.json").stat().st_mtime

        PluginRegistrar(repo).register_installed()
        assert (plugins_dir / "installed_plugins.json").stat().st_mtime == mtime


class TestInstallClaudePlugin:
    def test_registers_plugin_and_marketplace(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: tmp_path))
        repo = tmp_path / "teatree-clone"
        repo.mkdir()
        assert PluginRegistrar(repo).install() is True

        data = json.loads((tmp_path / ".claude" / "plugins" / "installed_plugins.json").read_text())
        assert "t3@souliane" in data["plugins"]
        assert data["plugins"]["t3@souliane"][0]["installPath"] == str(repo.resolve())

        settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
        assert settings["enabledPlugins"]["t3@souliane"] is True


class TestStripApmHooks:
    def test_no_file(self, tmp_path: Path) -> None:
        assert strip_apm_hooks(tmp_path / "nonexistent.json") == 0

    def test_no_hooks(self, tmp_path: Path) -> None:
        settings = tmp_path / "settings.json"
        settings.write_text(json.dumps({"key": "value"}))
        assert strip_apm_hooks(settings) == 0

    def test_removes_apm_entries(self, tmp_path: Path) -> None:
        settings = tmp_path / "settings.json"
        data = {
            "hooks": {
                "UserPromptSubmit": [
                    {"type": "command", "command": "my-hook"},
                    {"type": "command", "command": "apm-hook", "_apm_source": "teatree"},
                ],
            },
        }
        settings.write_text(json.dumps(data))
        removed = strip_apm_hooks(settings)
        assert removed == 1
        result = json.loads(settings.read_text())
        assert len(result["hooks"]["UserPromptSubmit"]) == 1
        assert result["hooks"]["UserPromptSubmit"][0]["command"] == "my-hook"

    def test_removes_empty_hook_keys(self, tmp_path: Path) -> None:
        settings = tmp_path / "settings.json"
        data = {
            "hooks": {
                "UserPromptSubmit": [
                    {"type": "command", "_apm_source": "teatree"},
                ],
            },
        }
        settings.write_text(json.dumps(data))
        removed = strip_apm_hooks(settings)
        assert removed == 1
        result = json.loads(settings.read_text())
        assert "hooks" not in result

    def test_invalid_json(self, tmp_path: Path) -> None:
        settings = tmp_path / "settings.json"
        settings.write_text("not json")
        assert strip_apm_hooks(settings) == 0

    def test_hooks_not_a_dict(self, tmp_path: Path) -> None:
        settings = tmp_path / "settings.json"
        settings.write_text(json.dumps({"hooks": "not-a-dict"}))
        assert strip_apm_hooks(settings) == 0
