"""``t3 config show`` — read-only intent + DB-cache view (issue #628).

Integration-first: a real ``~/.teatree.toml`` fixture under ``tmp_path``
with ``teatree.config.CONFIG_PATH`` monkeypatched, exercised through the
typer ``CliRunner``. The build step (``build_config_view``) is unit-tested
for the cache-vs-intent partition because that classification is the
load-bearing logic the #628 invariant turns on.
"""

import json
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from teatree.cli import app
from teatree.cli.config_view import build_config_view

runner = CliRunner()


def _write_toml(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


class TestBuildConfigView:
    def test_intent_section_reflects_resolved_settings(self, tmp_path: Path) -> None:
        config_path = tmp_path / ".teatree.toml"
        _write_toml(config_path, '[teatree]\nmode = "auto"\ncontribute = true\n')
        with patch("teatree.config.CONFIG_PATH", config_path):
            view = build_config_view()
        assert view.intent["mode"] == "auto"
        assert view.intent["contribute"] is True
        assert view.config_path == str(config_path)
        assert view.config_exists is True

    def test_missing_config_is_reported_not_crashed(self, tmp_path: Path) -> None:
        with patch("teatree.config.CONFIG_PATH", tmp_path / "absent.toml"):
            view = build_config_view()
        assert view.config_exists is False
        # Defaults still resolve so the view is never empty.
        assert view.intent["mode"] == "interactive"

    def test_derived_section_is_labelled_regenerable_cache(self, tmp_path: Path) -> None:
        with patch("teatree.config.CONFIG_PATH", tmp_path / "absent.toml"):
            view = build_config_view()
        # Every derived entry must be flagged regenerable so the #628
        # cache-vs-intent invariant is visible in the output itself.
        assert view.derived  # non-empty
        assert all(entry["regenerable"] is True for entry in view.derived)

    def test_db_path_is_surfaced_in_derived(self, tmp_path: Path) -> None:
        with patch("teatree.config.CONFIG_PATH", tmp_path / "absent.toml"):
            view = build_config_view()
        names = {entry["name"] for entry in view.derived}
        assert "control DB" in names


class TestConfigShowCommand:
    def test_human_readable_output_partitions_intent_and_cache(self, tmp_path: Path) -> None:
        config_path = tmp_path / ".teatree.toml"
        _write_toml(config_path, '[teatree]\nmode = "auto"\n')
        with patch("teatree.config.CONFIG_PATH", config_path):
            result = runner.invoke(app, ["config", "show"])
        assert result.exit_code == 0
        assert "Intent" in result.stdout
        assert "source of truth" in result.stdout
        assert "regenerable cache" in result.stdout
        assert "mode" in result.stdout
        assert "auto" in result.stdout

    def test_json_output_is_machine_readable(self, tmp_path: Path) -> None:
        config_path = tmp_path / ".teatree.toml"
        _write_toml(config_path, '[teatree]\nmode = "auto"\n')
        with patch("teatree.config.CONFIG_PATH", config_path):
            result = runner.invoke(app, ["config", "show", "--json"])
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["intent"]["mode"] == "auto"
        assert payload["config_exists"] is True
        assert isinstance(payload["derived"], list)

    def test_show_is_read_only_no_config_written(self, tmp_path: Path) -> None:
        config_path = tmp_path / ".teatree.toml"
        _write_toml(config_path, '[teatree]\nmode = "interactive"\n')
        before = config_path.read_text(encoding="utf-8")
        with patch("teatree.config.CONFIG_PATH", config_path):
            runner.invoke(app, ["config", "show"])
        assert config_path.read_text(encoding="utf-8") == before
