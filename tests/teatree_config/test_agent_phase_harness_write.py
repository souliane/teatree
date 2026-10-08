"""``agent_phase_harness`` is refused at write time unless every value pins a known harness or unpins."""

import json
import sqlite3
from contextlib import closing
from io import StringIO
from pathlib import Path

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.agents.model_tiering import resolve_phase_harness
from teatree.config.write_validation import ConfigWriteError, validate_config_write
from teatree.core.models import ConfigSetting


def _seed_cold_config(db: Path, key: str, value: object) -> None:
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute("CREATE TABLE teatree_config_setting (id INTEGER PRIMARY KEY, scope TEXT, key TEXT, value TEXT)")
        conn.execute(
            "INSERT INTO teatree_config_setting (scope, key, value) VALUES ('', ?, ?)", (key, json.dumps(value))
        )


class TestAgentPhaseHarnessWrite(TestCase):
    @pytest.fixture(autouse=True)
    def _inject(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch

    def test_a_harness_outside_the_closed_set_is_refused_naming_the_valid_values(self) -> None:
        with pytest.raises(ConfigWriteError, match="valid values: claude_sdk, pydantic_ai"):
            validate_config_write("agent_phase_harness", {"coding": "codex_app_server"})

    def test_the_refused_value_is_never_stored(self) -> None:
        with pytest.raises(SystemExit):
            call_command(
                "config_setting",
                "set",
                "agent_phase_harness",
                '{"coding": "codex_app_server"}',
                stdout=StringIO(),
                stderr=StringIO(),
            )

        assert not ConfigSetting.objects.filter(key="agent_phase_harness").exists()

    def test_an_unpin_is_accepted_and_lets_the_routed_harness_run_the_phase(self) -> None:
        assert validate_config_write("agent_phase_harness", {"testing": "inherit"}) == {"testing": "inherit"}
        db = self.tmp_path / "config.sqlite3"
        _seed_cold_config(db, "agent_phase_harness", {"testing": "inherit"})
        self.monkeypatch.setenv("T3_CONFIG_DB", str(db))

        assert resolve_phase_harness("codex_app_server", "testing") == "codex_app_server"

    def test_without_a_row_the_testing_phase_stays_pinned_to_claude(self) -> None:
        assert resolve_phase_harness("codex_app_server", "testing") == "claude_sdk"
