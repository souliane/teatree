# test-path: cross-cutting
"""DB-home ``autoload`` resolution off the ``ConfigSetting`` store + the cold reader.

eliminate-~/.teatree.toml: the #256 engagement flag ``autoload`` moved from
TOML-home to DB-home. Its Django-side reader resolves it via
``get_effective_settings`` (env -> DB overlay -> DB global -> default); a
``[teatree] autoload`` TOML value is ignored on read. Its cold pre-Django readers
-- ``hooks.scripts.teatree_settings.autoload_enabled`` (Python, via the Django-free
``_cold_db_bool``) and the bash ``statusline.sh`` gate -- read the canonical sqlite
directly, so it needs no TOML.

Integration-first: real ``ConfigSetting`` rows + real env for the Django path, a
real sqlite file for the cold path; no mocks. HOME / ``Path.home`` are sandboxed so
the suite never touches the developer's real home.
"""

import json
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from django.test import TestCase

import teatree.config as config_facade
from hooks.scripts.teatree_settings import autoload_enabled
from teatree.config import get_effective_settings
from teatree.core.models import ConfigSetting


class _AutoloadCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        sandbox = Path(tempfile.mkdtemp(prefix="teatree-autoload-"))
        self.home = sandbox / "home"
        self.home.mkdir(parents=True)
        self.addCleanup(lambda: shutil.rmtree(sandbox, ignore_errors=True))
        self.enterContext(patch.dict(os.environ))
        self.enterContext(patch.object(Path, "home", return_value=self.home))
        # No real ~/.teatree.toml leaks into the resolver — point CONFIG_PATH at a
        # non-existent sandbox file so ``load_config`` returns dataclass defaults.
        self.enterContext(patch.object(config_facade, "CONFIG_PATH", self.home / ".teatree.toml"))
        os.environ["HOME"] = str(self.home)
        os.environ.pop("T3_AUTOLOAD", None)
        os.environ.pop("T3_OVERLAY_NAME", None)


class TestDbConfigSetting(_AutoloadCase):
    def test_no_row_defaults_to_off(self) -> None:
        assert get_effective_settings().autoload is False

    def test_global_scope_row_enables(self) -> None:
        ConfigSetting.objects.set_value("autoload", value=True, scope="")
        assert get_effective_settings().autoload is True

    def test_global_scope_row_false_keeps_off(self) -> None:
        ConfigSetting.objects.set_value("autoload", value=False, scope="")
        assert get_effective_settings().autoload is False

    def test_overlay_scope_row_beats_global_scope(self) -> None:
        os.environ["T3_OVERLAY_NAME"] = "myoverlay"
        ConfigSetting.objects.set_value("autoload", value=False, scope="")
        ConfigSetting.objects.set_value("autoload", value=True, scope="myoverlay")
        assert get_effective_settings().autoload is True


class TestEnvOverrideWins(_AutoloadCase):
    def test_env_truthy_beats_absent_row(self) -> None:
        os.environ["T3_AUTOLOAD"] = "1"
        assert get_effective_settings().autoload is True

    def test_env_falsey_beats_db_true(self) -> None:
        ConfigSetting.objects.set_value("autoload", value=True, scope="")
        os.environ["T3_AUTOLOAD"] = "false"
        assert get_effective_settings().autoload is False


def _make_config_db(path: Path, *, autoload: object) -> None:
    """Build a real ``teatree_config_setting`` sqlite carrying a GLOBAL ``autoload`` row."""
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE teatree_config_setting ("
            "id INTEGER PRIMARY KEY, scope TEXT NOT NULL DEFAULT '', "
            "key TEXT NOT NULL, value TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO teatree_config_setting (scope, key, value) VALUES ('', 'autoload', ?)",
            (json.dumps(autoload),),
        )
        conn.commit()
    finally:
        conn.close()


class TestColdAutoloadEnabled:
    """The Django-free cold reader ``hooks.scripts.teatree_settings.autoload_enabled``.

    The SessionStart / UserPromptSubmit hooks consult this pre-Django to decide
    default-off engagement: ``T3_AUTOLOAD`` env first, else the canonical sqlite
    (via ``cold_reader``), else OFF. No TOML fallback (autoload is DB-home).
    """

    @pytest.fixture(autouse=True)
    def _clean_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # ``_isolate_env`` (conftest) already sandboxes HOME and clears T3_CONFIG_DB /
        # XDG_DATA_HOME; just drop the env short-circuit so the DB path is exercised.
        monkeypatch.delenv("T3_AUTOLOAD", raising=False)

    def test_db_true_enables(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        db = tmp_path / "db.sqlite3"
        _make_config_db(db, autoload=True)
        monkeypatch.setenv("T3_CONFIG_DB", str(db))
        assert autoload_enabled() is True

    def test_db_false_disables(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        db = tmp_path / "db.sqlite3"
        _make_config_db(db, autoload=False)
        monkeypatch.setenv("T3_CONFIG_DB", str(db))
        assert autoload_enabled() is False

    def test_missing_db_fails_closed_off(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_CONFIG_DB", str(tmp_path / "absent.sqlite3"))
        assert autoload_enabled() is False

    def test_env_truthy_wins_over_absent_db(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_AUTOLOAD", "1")
        assert autoload_enabled() is True

    def test_teatree_toml_autoload_is_ignored(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # autoload is DB-home: a ``[teatree] autoload`` value is NOT read (no TOML
        # fallback). With no DB row the cold reader fails closed to OFF.
        home = tmp_path / "toml-home"
        home.mkdir()
        (home / ".teatree.toml").write_text("[teatree]\nautoload = true\n", encoding="utf-8")
        monkeypatch.setenv("HOME", str(home))
        assert autoload_enabled() is False
