"""End-to-end: the DB-home config seed feeds the flipped cold-hook readers.

The coupled proof for the DB-home cutover: a NON-default gate value AND a raised
integer budget are written into the ``ConfigSetting`` store (the seed a fresh
setup performs, one :meth:`ConfigSetting.objects.set_value` per key), then read
back through the FLIPPED ``teatree_settings`` / ``cold_reader``.

So the test exercises the ORM seed -> snapshot -> stdlib cold read chain with no
mocks of any half. RED if the seed drops a cold-hook key (the row is absent) OR
the reader stays on a stale source, which is the whole point of the cutover.
"""

import sqlite3
from pathlib import Path

import pytest
from django.db import connection
from django.test import TransactionTestCase

from teatree.config import cold_reader
from teatree.core.models import ConfigSetting


def _snapshot_db_to_file(path: Path) -> None:
    """Copy the live (committed) test DB to ``path`` so the stdlib cold reader can open it."""
    dest = sqlite3.connect(path)
    try:
        connection.connection.backup(dest)
    finally:
        dest.close()


class TestSeedThenColdRead(TransactionTestCase):
    """``TransactionTestCase`` so the seeded rows are committed and the snapshot sees them.

    The cold reader opens a byte-for-byte snapshot through a SEPARATE sqlite
    connection, so the seed must be a real commit (not a rolled-back ``TestCase``
    transaction) for the backup to capture it.
    """

    @pytest.fixture(autouse=True)
    def _clean_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Clear the cold-read DB env so a non-default verdict can only come from the seeded row."""
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch
        monkeypatch.delenv("T3_CONFIG_DB", raising=False)
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)

    def _run_setup_seed(self, raw: dict) -> None:
        """Seed the store the way a fresh setup does — one ``set_value`` per global key."""
        for key, value in raw.get("teatree", {}).items():
            ConfigSetting.objects.set_value(key, value)

    def test_seeded_nondefault_gate_and_budget_read_through_cold_layer(self) -> None:
        self._run_setup_seed(
            {
                "teatree": {
                    "memory_recall_enabled": False,  # default True -> disabled gate
                    "self_dm_gate_enabled": False,  # default True -> disabled gate
                    "deny_circuit_breaker_threshold": 7,  # default 3 -> raised budget
                }
            }
        )

        # The seed half: every cold-hook key landed a row in the GLOBAL scope.
        assert ConfigSetting.objects.get_effective("memory_recall_enabled") is False
        assert ConfigSetting.objects.get_effective("self_dm_gate_enabled") is False
        assert ConfigSetting.objects.get_effective("deny_circuit_breaker_threshold") == 7

        db_file = self.tmp_path / "db.sqlite3"
        _snapshot_db_to_file(db_file)
        self.monkeypatch.setenv("T3_CONFIG_DB", str(db_file))

        from hooks.scripts import teatree_settings  # noqa: PLC0415

        # The flipped bool adapter returns the SEEDED non-default, not the in-code
        # default — so the gate stays disabled exactly as the user configured.
        assert teatree_settings.teatree_bool_setting("memory_recall_enabled", default=True) is False
        assert teatree_settings.teatree_bool_setting("self_dm_gate_enabled", default=True) is False

        # The cold reader returns the SEEDED raised budget. The hook_router int flip
        # is PR4; here the cold reader proves the int budget was seeded losslessly
        # and is cold-readable as the correct int type.
        assert cold_reader.int_setting("deny_circuit_breaker_threshold", default=3, minimum=1) == 7

    def test_unseeded_gate_falls_to_in_code_default_after_seed(self) -> None:
        # A gate the seed did not configure has no seeded row, so the flipped reader
        # falls to its in-code default — a missing row never disables a default-on
        # gate (the fail-open parity crux).
        self._run_setup_seed({"teatree": {"memory_recall_enabled": False}})
        db_file = self.tmp_path / "db.sqlite3"
        _snapshot_db_to_file(db_file)
        self.monkeypatch.setenv("T3_CONFIG_DB", str(db_file))

        from hooks.scripts import teatree_settings  # noqa: PLC0415

        assert teatree_settings.teatree_bool_setting("completion_claim_gate_enabled", default=True) is True
