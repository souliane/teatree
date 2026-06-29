"""End-to-end: the t3-setup TOML->DB seed feeds the flipped cold-hook readers (config-unify PR3).

The coupled proof for the migration, driven through the REAL setup-seed path. A
genuine on-disk config TOML carrying a NON-default gate value AND a raised integer
budget is seeded into the ``ConfigSetting`` store through the SAME management
command ``t3 setup`` runs — ``config_setting import --no-clobber``
(:func:`teatree.self_update.seed_db_config_from_toml` shells out to exactly this),
which calls :func:`import_toml_into_db` with ``clobber=False`` on
``load_config().raw``. The seeded rows are then read back through the FLIPPED
``teatree_settings`` / ``cold_reader``.

So the test exercises the file -> ``load_config().raw`` -> command -> ORM seed ->
snapshot -> stdlib cold read chain with no mocks of any half and no hand-built
``raw`` dict — the planted TOML is the only input. RED if the seed drops a
cold-hook key (the row is absent) OR the reader stays on TOML (the clean ``$HOME``
carries no value to fall back to), which is the whole point of PR3.
"""

import sqlite3
from io import StringIO
from pathlib import Path

import pytest
from django.core.management import call_command
from django.db import connection
from django.test import TransactionTestCase

import teatree.config as config_facade
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
    def _clean_home_and_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A real planted seed TOML the command reads, kept OUTSIDE a clean ``$HOME``.

        Anti-vacuity: the seed reads its TOML via the monkeypatched ``CONFIG_PATH``,
        but the flipped ``teatree_settings`` reader falls back to ``Path.home() /
        ".teatree.toml"``. Planting the seed file OUTSIDE ``$HOME`` keeps that
        fallback file ABSENT — so a non-default verdict from the flipped reader can
        ONLY come from the seeded DB row, and a no-op flip (the reader staying on
        TOML) turns the test red instead of silently passing off the seed file.
        """
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch
        home = tmp_path / "home"
        home.mkdir(exist_ok=True)
        seed_dir = tmp_path / "seed-config"
        seed_dir.mkdir(exist_ok=True)
        self.config_path = seed_dir / ".teatree.toml"
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setattr(config_facade, "CONFIG_PATH", self.config_path)
        monkeypatch.delenv("T3_CONFIG_DB", raising=False)
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)

    def _run_setup_seed(self) -> None:
        """Run the EXACT command ``t3 setup``'s ``seed_db_config_from_toml`` invokes."""
        call_command("config_setting", "import", "--no-clobber", stdout=StringIO())

    def test_seeded_nondefault_gate_and_budget_read_through_cold_layer(self) -> None:
        self.config_path.write_text(
            "[teatree]\n"
            "memory_recall_enabled = false\n"  # default True -> disabled gate
            "self_dm_gate_enabled = false\n"  # default True -> disabled gate
            "deny_circuit_breaker_threshold = 7\n",  # default 3 -> raised budget
            encoding="utf-8",
        )
        self._run_setup_seed()

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
        # A gate the TOML did not configure has no seeded row, so the flipped reader
        # falls to its in-code default — a missing row never disables a default-on
        # gate (the fail-open parity crux).
        self.config_path.write_text("[teatree]\nmemory_recall_enabled = false\n", encoding="utf-8")
        self._run_setup_seed()
        db_file = self.tmp_path / "db.sqlite3"
        _snapshot_db_to_file(db_file)
        self.monkeypatch.setenv("T3_CONFIG_DB", str(db_file))

        from hooks.scripts import teatree_settings  # noqa: PLC0415

        assert teatree_settings.teatree_bool_setting("completion_claim_gate_enabled", default=True) is True
