"""``t3 <overlay> speed`` — show / set the throughput dial.

Integration-first: a real ``~/.teatree.toml`` fixture under ``tmp_path`` with
``teatree.config.CONFIG_PATH`` monkeypatched, exercised through the typer
``CliRunner`` against the same ``speed`` subgroup the overlay app builder
attaches via :func:`teatree.cli.speed.register_speed_commands`.
"""

from pathlib import Path
from unittest.mock import patch

import typer
from typer.testing import CliRunner

from teatree.cli.speed import register_speed_commands
from teatree.config import Speed

runner = CliRunner()


def _app() -> typer.Typer:
    app = typer.Typer()
    register_speed_commands(app)
    return app


class TestSpeedSet:
    def test_set_writes_global_speed_key(self, tmp_path: Path) -> None:
        config_path = tmp_path / ".teatree.toml"
        config_path.write_text("[teatree]\n", encoding="utf-8")
        with patch("teatree.config.CONFIG_PATH", config_path):
            result = runner.invoke(_app(), ["speed", "set", "full"])
        assert result.exit_code == 0
        assert 'speed = "full"' in config_path.read_text(encoding="utf-8")
        assert "full" in result.stdout

    def test_set_creates_config_when_absent(self, tmp_path: Path) -> None:
        config_path = tmp_path / ".teatree.toml"
        with patch("teatree.config.CONFIG_PATH", config_path):
            result = runner.invoke(_app(), ["speed", "set", "boost"])
        assert result.exit_code == 0
        assert config_path.is_file()
        assert 'speed = "boost"' in config_path.read_text(encoding="utf-8")

    def test_set_alias_is_normalised_to_canonical(self, tmp_path: Path) -> None:
        config_path = tmp_path / ".teatree.toml"
        config_path.write_text("[teatree]\n", encoding="utf-8")
        with patch("teatree.config.CONFIG_PATH", config_path):
            result = runner.invoke(_app(), ["speed", "set", "high"])
        assert result.exit_code == 0
        # The canonical value is persisted, not the alias.
        assert 'speed = "full"' in config_path.read_text(encoding="utf-8")
        assert "high" not in config_path.read_text(encoding="utf-8")

    def test_set_preserves_other_keys(self, tmp_path: Path) -> None:
        config_path = tmp_path / ".teatree.toml"
        config_path.write_text('[teatree]\nmode = "auto"\nbranch_prefix = "ac-"\n', encoding="utf-8")
        with patch("teatree.config.CONFIG_PATH", config_path):
            runner.invoke(_app(), ["speed", "set", "slow"])
        body = config_path.read_text(encoding="utf-8")
        assert 'mode = "auto"' in body
        assert 'branch_prefix = "ac-"' in body
        assert 'speed = "slow"' in body

    def test_set_typo_is_rejected_and_writes_nothing(self, tmp_path: Path) -> None:
        config_path = tmp_path / ".teatree.toml"
        config_path.write_text("[teatree]\n", encoding="utf-8")
        before = config_path.read_text(encoding="utf-8")
        with patch("teatree.config.CONFIG_PATH", config_path):
            result = runner.invoke(_app(), ["speed", "set", "ludicrous"])
        assert result.exit_code == 1
        assert config_path.read_text(encoding="utf-8") == before


class TestSpeedShow:
    def test_show_reports_effective_value(self, tmp_path: Path) -> None:
        config_path = tmp_path / ".teatree.toml"
        config_path.write_text('[teatree]\nspeed = "full"\n', encoding="utf-8")
        with patch("teatree.config.CONFIG_PATH", config_path):
            result = runner.invoke(_app(), ["speed", "show"])
        assert result.exit_code == 0
        assert result.stdout.strip() == Speed.FULL.value

    def test_show_defaults_to_medium_when_unset(self, tmp_path: Path) -> None:
        config_path = tmp_path / ".teatree.toml"
        config_path.write_text("[teatree]\n", encoding="utf-8")
        with patch("teatree.config.CONFIG_PATH", config_path):
            result = runner.invoke(_app(), ["speed", "show"])
        assert result.exit_code == 0
        assert result.stdout.strip() == Speed.MEDIUM.value

    def test_show_is_read_only(self, tmp_path: Path) -> None:
        config_path = tmp_path / ".teatree.toml"
        config_path.write_text('[teatree]\nspeed = "slow"\n', encoding="utf-8")
        before = config_path.read_text(encoding="utf-8")
        with patch("teatree.config.CONFIG_PATH", config_path):
            runner.invoke(_app(), ["speed", "show"])
        assert config_path.read_text(encoding="utf-8") == before
