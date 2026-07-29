# test-path: cross-cutting
"""DB-home ``autoload`` resolution off the ``ConfigSetting`` store + the cold reader.

The #256 engagement flag ``autoload`` is DB-home. Its Django-side reader resolves
it via ``get_effective_settings`` (env -> DB overlay -> DB global -> default). Its
cold pre-Django readers -- ``hooks.scripts.teatree_settings.autoload_enabled``
(Python, via the Django-free ``_cold_db_bool``) and the bash ``statusline.sh`` gate
-- read the canonical sqlite directly.

Integration-first: real ``ConfigSetting`` rows + real env for the Django path, a
real sqlite file for the cold path; no mocks. HOME / ``Path.home`` are sandboxed so
the suite never touches the developer's real home.
"""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from django.test import TestCase

from hooks.scripts import teatree_settings
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
    (via ``cold_reader``), else OFF.
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


# Runs in a REAL subprocess whose ``sys.path`` is stripped of every entry that could
# satisfy ``import teatree``, then imports the leaf under its BARE identity with only
# the scripts dir on the path -- exactly how the live hook reaches it. Reports the
# resolved value as JSON so the parent can assert on it.
_NO_TEATREE_PROBE = """
import json, sys
from pathlib import Path

sys.path[:] = [p for p in sys.path if p and not (Path(p) / "teatree" / "__init__.py").is_file()]
try:
    import teatree
except ModuleNotFoundError:
    pass
else:
    print(json.dumps({{"error": "probe is void: teatree still importable from {{}}".format(teatree.__file__)}}))
    raise SystemExit(0)

sys.path.insert(0, {scripts_dir!r})
from teatree_settings import autoload_enabled, teatree_int_setting

print(json.dumps({{"autoload": autoload_enabled(), "budget": teatree_int_setting("probe_budget", default=7)}}))
"""


class TestColdReadersBootstrapSrc:
    """#3499: the cold readers must reach the DB when ``teatree`` is NOT already importable.

    ``run-hook.sh`` execs the first ``python3.13|3.12|3.11|python3`` on PATH. Where that
    resolves to a bare system interpreter, ``teatree`` is not installed in it, and
    ``hook_router`` puts only the PLUGIN ROOT -- never the sibling ``src/`` -- on
    ``sys.path``. So the lazy ``from teatree.config.cold_reader import read_setting``
    raised ``ModuleNotFoundError``, the blanket ``except`` swallowed it, and EVERY
    DB-home flag in the module silently resolved to its compiled-in default: a total
    DB-read outage indistinguishable from "the operator never opted in". The observed
    damage was ``autoload`` reading OFF while the store said ``True``, so no session
    ever auto-engaged and every ``gate <name> disable/enable`` write was inert.

    In-process tests cannot catch this -- pytest always has ``teatree`` importable --
    hence the subprocess.
    """

    @staticmethod
    def _run_probe(db: Path, tmp_path: Path) -> dict[str, object]:
        scripts_dir = Path(teatree_settings.__file__).resolve().parent
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(tmp_path),
            "T3_CONFIG_DB": str(db),
        }
        proc = subprocess.run(
            [sys.executable, "-c", _NO_TEATREE_PROBE.format(scripts_dir=str(scripts_dir))],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
            check=True,
        )
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
        assert "error" not in payload, payload["error"]
        return payload

    def test_db_true_enables_without_teatree_on_syspath(self, tmp_path: Path) -> None:
        db = tmp_path / "db.sqlite3"
        _make_config_db(db, autoload=True)
        assert self._run_probe(db, tmp_path)["autoload"] is True

    def test_int_budget_reads_db_without_teatree_on_syspath(self, tmp_path: Path) -> None:
        """The bootstrap lives at the SHARED seam, so the int reader is fixed too.

        ``7`` is the caller's default and ``11`` the stored row, so a value of ``11``
        can only come from a DB read that actually succeeded.
        """
        db = tmp_path / "db.sqlite3"
        _make_config_db(db, autoload=True)
        conn = sqlite3.connect(db)
        try:
            conn.execute(
                "INSERT INTO teatree_config_setting (scope, key, value) VALUES ('', 'probe_budget', ?)",
                (json.dumps(11),),
            )
            conn.commit()
        finally:
            conn.close()
        assert self._run_probe(db, tmp_path)["budget"] == 11
