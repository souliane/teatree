# test-path: cross-cutting
"""DB-home ``overlays`` / ``e2e_repos`` registries off the cold ``ConfigSetting`` store.

eliminate-~/.teatree.toml: the two NON-``UserSettings`` config tables read directly
off ``config.raw`` — the ``overlays`` overlay-definition registry (``discover_overlays``)
and the ``e2e_repos`` registry (``load_e2e_repos``) — moved from TOML-home to DB-home.
``load_config._inject_db_registries`` reads each as one JSON-dict row via the Django-free
``cold_reader`` and overrides ``raw[key]``, so every existing reader is untouched and an
install with NO ``~/.teatree.toml`` still discovers its overlays + e2e repos.

Integration-first: a real sqlite file at ``T3_CONFIG_DB`` (the canonical cold-path store),
no mocks beyond ``entry_points`` (installed overlay packages would otherwise leak into
discovery). ``_isolate_env`` (conftest) clears ``T3_CONFIG_DB`` / ``XDG_DATA_HOME`` so each
test seeds its own store.
"""

import json
import sqlite3
from pathlib import Path

import pytest

from teatree.config import discover_overlays, load_config
from teatree.config.loader import load_e2e_repos

from ._shared import _write_toml


def _seed_registry_db(path: Path, **rows: object) -> None:
    """Build a real ``teatree_config_setting`` sqlite carrying GLOBAL registry rows."""
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE teatree_config_setting ("
            "id INTEGER PRIMARY KEY, scope TEXT NOT NULL DEFAULT '', "
            "key TEXT NOT NULL, value TEXT NOT NULL)"
        )
        for key, value in rows.items():
            conn.execute(
                "INSERT INTO teatree_config_setting (scope, key, value) VALUES ('', ?, ?)",
                (key, json.dumps(value)),
            )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def _no_entry_point_overlays(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("importlib.metadata.entry_points", lambda **_kw: [])


@pytest.mark.usefixtures("_no_entry_point_overlays")
def test_discover_overlays_from_db_registry_without_toml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "db.sqlite3"
    _seed_registry_db(db, overlays={"db-overlay": {"class": "dbpkg.settings"}})
    monkeypatch.setenv("T3_CONFIG_DB", str(db))

    result = discover_overlays(config_path=tmp_path / "absent.toml")

    by_name = {e.name: e for e in result}
    assert "db-overlay" in by_name
    assert by_name["db-overlay"].overlay_class == "dbpkg.settings"


@pytest.mark.usefixtures("_no_entry_point_overlays")
def test_db_registry_overrides_toml_overlays(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The DB row is authoritative: a stored ``overlays`` registry row WINS over a
    # ``[overlays.<name>]`` TOML table (the file is the pre-migration fallback only).
    config_path = tmp_path / ".teatree.toml"
    _write_toml(config_path, '[overlays.toml-overlay]\nclass = "toml.settings"\n')
    db = tmp_path / "db.sqlite3"
    _seed_registry_db(db, overlays={"db-overlay": {"class": "dbpkg.settings"}})
    monkeypatch.setenv("T3_CONFIG_DB", str(db))

    names = {e.name for e in discover_overlays(config_path=config_path)}

    assert names == {"db-overlay"}


def test_load_e2e_repos_from_db_registry_without_toml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "db.sqlite3"
    _seed_registry_db(db, e2e_repos={"myrepo": {"url": "git@x:r.git", "branch": "dev", "e2e_dir": "tests"}})
    monkeypatch.setenv("T3_CONFIG_DB", str(db))

    repos = load_e2e_repos(path=tmp_path / "absent.toml")

    assert len(repos) == 1
    assert repos[0].name == "myrepo"
    assert repos[0].url == "git@x:r.git"
    assert repos[0].branch == "dev"
    assert repos[0].e2e_dir == "tests"


def test_load_config_with_no_toml_boots_from_db_registries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The boot-with-zero-toml guard: with the file absent, ``load_config`` still
    # populates both registry tables from the DB store and never raises.
    db = tmp_path / "db.sqlite3"
    _seed_registry_db(
        db,
        overlays={"db-overlay": {"class": "dbpkg.settings"}},
        e2e_repos={"myrepo": {"url": "git@x:r.git"}},
    )
    monkeypatch.setenv("T3_CONFIG_DB", str(db))

    config = load_config(tmp_path / "absent.toml")

    assert config.raw["overlays"] == {"db-overlay": {"class": "dbpkg.settings"}}
    assert config.raw["e2e_repos"] == {"myrepo": {"url": "git@x:r.git"}}


def test_load_config_no_toml_no_db_is_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Fail-open: an install with neither a file nor a DB registry row boots with an
    # empty ``raw`` — no overlays, no e2e repos, no crash.
    monkeypatch.setenv("T3_CONFIG_DB", str(tmp_path / "absent.sqlite3"))

    config = load_config(tmp_path / "absent.toml")

    assert "overlays" not in config.raw
    assert "e2e_repos" not in config.raw
