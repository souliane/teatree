"""The plugin-registration pass writes only under ``--repair``, and never over an unreadable file.

Two faults met here. It mutated on EVERY `t3 doctor check` — which the SessionStart
hook runs — contradicting ``--repair``'s own promise that a plain run never mutates.
And its JSON read degraded an unparsable file to ``{}``, so a momentarily-broken
``~/.claude/settings.json`` was replaced wholesale by a one-key file, destroying the
operator's permissions, hooks, env and statusLine block.
"""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from teatree.cli.doctor.plugin_repair import (
    _CLAUDE_PLUGIN_ID,
    UnparsableJson,
    _do_ensure_plugin_registered,
    _ensure_plugin_registered,
    _read_json_safe,
    _repair_enabled_plugins,
)

_FULL_SETTINGS = {
    "permissions": {"allow": ["Bash(git status)"]},
    "hooks": {"SessionStart": []},
    "statusLine": {"type": "command", "command": "/usr/local/bin/t3-statusline"},
}


class TestReadJsonSafeSeparatesAbsentFromUnparsable:
    def test_absent_file_is_an_empty_mapping(self, tmp_path: Path) -> None:
        assert _read_json_safe(tmp_path / "nothing.json") == {}

    def test_unparsable_file_is_the_sentinel(self, tmp_path: Path) -> None:
        path = tmp_path / "settings.json"
        path.write_text('{"permissions": {,}', encoding="utf-8")
        assert _read_json_safe(path) is UnparsableJson

    def test_readable_file_is_its_content(self, tmp_path: Path) -> None:
        path = tmp_path / "settings.json"
        path.write_text(json.dumps(_FULL_SETTINGS), encoding="utf-8")
        assert _read_json_safe(path) == _FULL_SETTINGS


class TestEnabledPluginsIsNotWrittenBlind:
    @staticmethod
    def _settings_at(home: Path, content: str) -> Path:
        path = home / ".claude" / "settings.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def test_a_plain_run_reports_drift_and_writes_nothing(self, tmp_path: Path) -> None:
        path = self._settings_at(tmp_path, json.dumps(_FULL_SETTINGS))
        with patch("teatree.cli.doctor.plugin_repair.Path.home", return_value=tmp_path):
            outcome = _repair_enabled_plugins()
        assert outcome.written is False
        assert _CLAUDE_PLUGIN_ID in outcome.detail
        assert json.loads(path.read_text(encoding="utf-8")) == _FULL_SETTINGS

    def test_repair_enables_the_plugin_and_keeps_every_other_key(self, tmp_path: Path) -> None:
        path = self._settings_at(tmp_path, json.dumps(_FULL_SETTINGS))
        with patch("teatree.cli.doctor.plugin_repair.Path.home", return_value=tmp_path):
            outcome = _repair_enabled_plugins(repair=True)
        assert outcome.written is True
        written = json.loads(path.read_text(encoding="utf-8"))
        assert written["enabledPlugins"][_CLAUDE_PLUGIN_ID] is True
        assert written["permissions"] == _FULL_SETTINGS["permissions"]
        assert written["statusLine"] == _FULL_SETTINGS["statusLine"]

    def test_an_unparsable_settings_file_is_never_written_over(self, tmp_path: Path) -> None:
        broken = '{"permissions": {"allow": ["Bash(git status)"],}'
        path = self._settings_at(tmp_path, broken)
        with patch("teatree.cli.doctor.plugin_repair.Path.home", return_value=tmp_path):
            outcome = _repair_enabled_plugins(repair=True)
        assert outcome.written is False
        assert "not readable JSON" in outcome.detail
        assert path.read_text(encoding="utf-8") == broken


class TestTheRegistrationPassHonoursRepair:
    def test_a_plain_run_writes_no_registration_file(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        home = tmp_path / "home"
        (home / ".claude" / "plugins").mkdir(parents=True)
        clone = tmp_path / "clone"
        clone.mkdir()
        with (
            patch("teatree.cli.doctor.plugin_repair.Path.home", return_value=home),
            patch("teatree.cli.doctor.plugin_repair._resolve_main_clone", return_value=clone),
        ):
            assert _do_ensure_plugin_registered() is True

        assert not list((home / ".claude" / "plugins").iterdir())
        assert not (home / ".claude" / "settings.json").exists()
        assert "--repair" in capsys.readouterr().out

    def test_repair_writes_all_three(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        (home / ".claude" / "plugins").mkdir(parents=True)
        clone = tmp_path / "clone"
        clone.mkdir()
        with (
            patch("teatree.cli.doctor.plugin_repair.Path.home", return_value=home),
            patch("teatree.cli.doctor.plugin_repair._resolve_main_clone", return_value=clone),
        ):
            assert _do_ensure_plugin_registered(repair=True) is True

        plugins = home / ".claude" / "plugins"
        assert (plugins / "known_marketplaces.json").is_file()
        assert (plugins / "installed_plugins.json").is_file()
        assert (home / ".claude" / "settings.json").is_file()


class TestAFilesystemFailureIsReportedNotSwallowed:
    """A repair that could not run must FAIL rather than report a repair that never happened."""

    def test_an_unwritable_plugins_root_reports_the_failure(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        home = tmp_path / "home"
        claude = home / ".claude"
        claude.mkdir(parents=True)
        # A FILE where the plugins directory belongs makes every write under it fail.
        (claude / "plugins").write_text("not a directory", encoding="utf-8")
        clone = tmp_path / "clone"
        clone.mkdir()
        with (
            patch("teatree.cli.doctor.plugin_repair.Path.home", return_value=home),
            patch("teatree.cli.doctor.plugin_repair._resolve_main_clone", return_value=clone),
        ):
            assert _ensure_plugin_registered(repair=True) is False
        assert "Could not repair" in capsys.readouterr().out
