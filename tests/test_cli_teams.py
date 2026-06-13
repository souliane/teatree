"""``t3 teams`` — on / off / status for the agent-teams master switch.

Integration-first: a real ``~/.teatree.toml`` fixture under ``tmp_path`` with
``teatree.config.CONFIG_PATH`` monkeypatched, exercised through the typer
``CliRunner`` against the same ``teams`` app the root CLI registers via
:data:`teatree.cli.teams.teams_app`.
"""

from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from teatree.cli.teams import teams_app

runner = CliRunner()


class TestTeamsOff:
    def test_off_writes_teams_enabled_false(self, tmp_path: Path) -> None:
        config_path = tmp_path / ".teatree.toml"
        config_path.write_text("[teams]\nenabled = true\n", encoding="utf-8")
        with patch("teatree.config.CONFIG_PATH", config_path):
            result = runner.invoke(teams_app, ["off"])
        assert result.exit_code == 0
        assert "enabled = false" in config_path.read_text(encoding="utf-8")

    def test_off_creates_config_when_absent(self, tmp_path: Path) -> None:
        config_path = tmp_path / ".teatree.toml"
        with patch("teatree.config.CONFIG_PATH", config_path):
            result = runner.invoke(teams_app, ["off"])
        assert result.exit_code == 0
        assert config_path.is_file()
        assert "enabled = false" in config_path.read_text(encoding="utf-8")


class TestTeamsOn:
    def test_on_writes_teams_enabled_true(self, tmp_path: Path) -> None:
        config_path = tmp_path / ".teatree.toml"
        config_path.write_text("[teams]\nenabled = false\n", encoding="utf-8")
        with patch("teatree.config.CONFIG_PATH", config_path):
            result = runner.invoke(teams_app, ["on"])
        assert result.exit_code == 0
        assert "enabled = true" in config_path.read_text(encoding="utf-8")

    def test_on_preserves_other_tables(self, tmp_path: Path) -> None:
        config_path = tmp_path / ".teatree.toml"
        config_path.write_text(
            '[teatree]\nmode = "auto"\n\n[teams]\nmax_panes = 3\n',
            encoding="utf-8",
        )
        with patch("teatree.config.CONFIG_PATH", config_path):
            runner.invoke(teams_app, ["on"])
        body = config_path.read_text(encoding="utf-8")
        assert 'mode = "auto"' in body
        assert "max_panes = 3" in body
        assert "enabled = true" in body


class TestTeamsStatus:
    def test_status_reflects_enabled(self, tmp_path: Path) -> None:
        config_path = tmp_path / ".teatree.toml"
        config_path.write_text("[teams]\nenabled = true\n", encoding="utf-8")
        with patch("teatree.config.CONFIG_PATH", config_path):
            result = runner.invoke(teams_app, ["status"])
        assert result.exit_code == 0
        assert "on" in result.stdout.lower()

    def test_status_reflects_disabled_with_classic_note(self, tmp_path: Path) -> None:
        config_path = tmp_path / ".teatree.toml"
        config_path.write_text("[teams]\nenabled = false\n", encoding="utf-8")
        with patch("teatree.config.CONFIG_PATH", config_path):
            result = runner.invoke(teams_app, ["status"])
        assert result.exit_code == 0
        lowered = result.stdout.lower()
        assert "off" in lowered
        assert "classic" in lowered
        assert "sub-agent" in lowered

    def test_status_defaults_to_off_when_unset(self, tmp_path: Path) -> None:
        config_path = tmp_path / ".teatree.toml"
        config_path.write_text("[teatree]\n", encoding="utf-8")
        with patch("teatree.config.CONFIG_PATH", config_path):
            result = runner.invoke(teams_app, ["status"])
        assert result.exit_code == 0
        assert "off" in result.stdout.lower()

    def test_status_is_read_only(self, tmp_path: Path) -> None:
        config_path = tmp_path / ".teatree.toml"
        config_path.write_text("[teams]\nenabled = true\n", encoding="utf-8")
        before = config_path.read_text(encoding="utf-8")
        with patch("teatree.config.CONFIG_PATH", config_path):
            runner.invoke(teams_app, ["status"])
        assert config_path.read_text(encoding="utf-8") == before


class TestStatusReflectsRoundTrip:
    def test_off_then_status_reports_off(self, tmp_path: Path) -> None:
        config_path = tmp_path / ".teatree.toml"
        config_path.write_text("[teams]\nenabled = true\n", encoding="utf-8")
        with patch("teatree.config.CONFIG_PATH", config_path):
            assert runner.invoke(teams_app, ["off"]).exit_code == 0
            status = runner.invoke(teams_app, ["status"])
        assert status.exit_code == 0
        assert "off" in status.stdout.lower()

    def test_on_then_status_reports_on(self, tmp_path: Path) -> None:
        config_path = tmp_path / ".teatree.toml"
        config_path.write_text("[teams]\nenabled = false\n", encoding="utf-8")
        with patch("teatree.config.CONFIG_PATH", config_path):
            assert runner.invoke(teams_app, ["on"]).exit_code == 0
            status = runner.invoke(teams_app, ["status"])
        assert status.exit_code == 0
        assert "on" in status.stdout.lower()
