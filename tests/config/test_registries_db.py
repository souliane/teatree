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

import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from teatree.config import discover_overlays, load_config, override_read_health
from teatree.config.loader import load_e2e_repos

from ._shared import _seed_config_db

_LOCK_HELD_SECONDS = 0.4


@contextmanager
def _a_writer_holding_the_store_locked(db: Path) -> Iterator[None]:
    """Hold EXCLUSIVE on *db* for `_LOCK_HELD_SECONDS`, the way a committing writer does.

    Entered only once the lock is actually held, so the read inside the block cannot pass by
    racing ahead of the writer and testing nothing.
    """
    held = threading.Event()

    def _hold() -> None:
        writer = sqlite3.connect(db, isolation_level=None)
        try:
            writer.execute("BEGIN EXCLUSIVE")
            held.set()
            time.sleep(_LOCK_HELD_SECONDS)
            writer.execute("ROLLBACK")
        finally:
            writer.close()

    thread = threading.Thread(target=_hold)
    thread.start()
    held.wait(timeout=5)
    try:
        yield
    finally:
        thread.join()


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


class TestAnUnreadableStoreIsNotAnEmptyRegistry:
    """An errored store and an empty one give the same ``raw`` — never the same silence.

    An unreadable store makes every configured overlay vanish, which surfaces downstream as
    "unknown overlay" rather than as the config fault it is. Boot must still proceed, so the
    fault is recorded where ``t3 doctor`` reads it instead of being raised.
    """

    def test_a_corrupt_store_records_the_degraded_read(self, config_db: Path) -> None:
        _seed_config_db(config_db, overlays={"db-overlay": {"class": "dbpkg.settings"}})
        assert load_config().raw["overlays"], "control: a readable store must populate the registry"
        override_read_health.clear_degraded_read()
        config_db.write_bytes(b"not a sqlite database at all")

        config = load_config()

        assert "overlays" not in config.raw
        report = override_read_health.degraded_read_report()
        assert report is not None, "an errored registry read must not resolve to a silent absence"
        assert any("_inject_db_registries" in caller for caller in report.callers)

    def test_an_absent_row_records_nothing(self, config_db: Path) -> None:
        _seed_config_db(config_db, e2e_repos={"myrepo": {"url": "git@x:r.git"}})
        override_read_health.clear_degraded_read()

        assert "overlays" not in load_config().raw
        assert override_read_health.degraded_read_report() is None


class TestAMomentarilyLockedStoreIsNotAnUnreadableOne:
    """Contention is transient by construction, so giving up on it reports a fault that is not one.

    The control DB runs `journal_mode=TRUNCATE`, so a reader genuinely waits on a committing
    writer; a cold read that gave up after 100ms recorded every such commit as a degraded config
    tier, and the safety clamp then resolved every autonomy/approval gate to its most restrictive
    value against a store that was perfectly readable a moment later.
    """

    def test_a_held_lock_neither_empties_the_registry_nor_records_a_fault(self, config_db: Path) -> None:
        registry = {"db-overlay": {"class": "dbpkg.settings"}}
        _seed_config_db(config_db, overlays=registry)
        override_read_health.clear_degraded_read()

        with _a_writer_holding_the_store_locked(config_db):
            config = load_config()

        assert config.raw["overlays"] == registry
        assert override_read_health.degraded_read_report() is None
