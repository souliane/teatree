# test-path: cross-cutting
"""DB-home ``overlays`` / ``e2e_repos`` registries off the cold ``ConfigSetting`` store.

The two NON-``UserSettings`` config tables read directly off ``config.raw`` — the
``overlays`` overlay-definition registry (``discover_overlays``) and the
``e2e_repos`` registry (``load_e2e_repos``) — are DB-home.
``load_config._inject_db_registries`` reads each as one JSON-dict row via the
Django-free ``cold_reader`` and populates ``raw[key]``, so every existing reader is
untouched and an install with no config file still discovers its overlays + e2e repos.

Integration-first: a real sqlite file at ``T3_CONFIG_DB`` (the canonical cold-path
store), no mocks beyond ``entry_points`` (installed overlay packages would otherwise
leak into discovery). ``_isolate_env`` (conftest) clears ``T3_CONFIG_DB`` /
``XDG_DATA_HOME`` so each test seeds its own store.
"""

from pathlib import Path

import pytest

from teatree.config import discover_overlays, load_config
from teatree.config.loader import load_e2e_repos

from ._shared import _seed_config_db


@pytest.fixture
def _no_entry_point_overlays(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("importlib.metadata.entry_points", lambda **_kw: [])


@pytest.mark.usefixtures("_no_entry_point_overlays")
def test_discover_overlays_from_db_registry(config_db: Path) -> None:
    _seed_config_db(config_db, overlays={"db-overlay": {"class": "dbpkg.settings"}})

    result = discover_overlays()

    by_name = {e.name: e for e in result}
    assert "db-overlay" in by_name
    assert by_name["db-overlay"].overlay_class == "dbpkg.settings"


def test_load_e2e_repos_from_db_registry(config_db: Path) -> None:
    _seed_config_db(config_db, e2e_repos={"myrepo": {"url": "git@x:r.git", "branch": "dev", "e2e_dir": "tests"}})

    repos = load_e2e_repos()

    assert len(repos) == 1
    assert repos[0].name == "myrepo"
    assert repos[0].url == "git@x:r.git"
    assert repos[0].branch == "dev"
    assert repos[0].e2e_dir == "tests"


def test_load_config_boots_from_db_registries(config_db: Path) -> None:
    _seed_config_db(
        config_db,
        overlays={"db-overlay": {"class": "dbpkg.settings"}},
        e2e_repos={"myrepo": {"url": "git@x:r.git"}},
    )

    config = load_config()

    assert config.raw["overlays"] == {"db-overlay": {"class": "dbpkg.settings"}}
    assert config.raw["e2e_repos"] == {"myrepo": {"url": "git@x:r.git"}}


def test_load_config_with_no_db_registry_is_empty(config_db: Path) -> None:
    del config_db  # no rows seeded -> the registries are simply absent

    config = load_config()

    assert "overlays" not in config.raw
    assert "e2e_repos" not in config.raw
