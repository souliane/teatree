"""Cold-hook overlay-registry readers resolve the migrated DB row, DB-only.

The ``overlays`` registry lives in the DB-home ``ConfigSetting`` store (the legacy
file tier is removed). Three Django-free cold-hook readers resolve it DB-only via
``cold_reader``. These tests build a REAL sqlite config DB (the same
``teatree_config_setting`` schema the Django migration creates) and prove each
reader returns the overlay config from that DB, and that the readers resolve to an
empty registry (never a weakened gate) when no config is present.

The three readers under test:

* ``managed_repo.load_protected_branches`` — overlay ``protected_branches``;
* ``managed_repo.overlay_managed_repo_signals`` — overlay repo slugs + ``path``;
* ``hook_router._self_dm_destination_ids`` — overlay Slack DM/user ids.
"""

import json
import sqlite3
from pathlib import Path

import pytest

import hooks.scripts.hook_router as router
from hooks.scripts import managed_repo


def _make_config_db(path: Path, *, overlays: dict[str, object]) -> None:
    """Build a real ``teatree_config_setting`` DB holding the ``overlays`` row at global scope."""
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE teatree_config_setting ("
            "id INTEGER PRIMARY KEY, scope TEXT NOT NULL DEFAULT '', "
            "key TEXT NOT NULL, value TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO teatree_config_setting (scope, key, value) VALUES ('', 'overlays', ?)",
            (json.dumps(overlays),),
        )
        conn.commit()
    finally:
        conn.close()


def _empty_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A clean HOME dir with no legacy config state — isolates the reader from real config."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    return home


def _no_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the cold reader at a non-existent DB so it resolves to an empty registry."""
    monkeypatch.setenv("T3_CONFIG_DB", str(tmp_path / "absent.sqlite3"))


class TestLoadProtectedBranchesDbFirst:
    def test_overlay_development_branch_from_db(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        db = tmp_path / "config.sqlite3"
        _make_config_db(db, overlays={"t3-acme": {"protected_branches": ["development", "release"]}})
        monkeypatch.setenv("T3_CONFIG_DB", str(db))
        _empty_home(tmp_path, monkeypatch)

        branches = managed_repo.load_protected_branches()

        assert "development" in branches
        assert "release" in branches
        assert {"main", "master"} <= branches


class TestOverlayManagedRepoSignalsDbFirst:
    def test_overlay_repo_and_path_from_db(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        overlay_path = tmp_path / "overlay-clone"
        overlay_path.mkdir()
        db = tmp_path / "config.sqlite3"
        _make_config_db(
            db,
            overlays={
                "t3-acme": {
                    "workspace_repos": ["acme-eng/widget-overlay"],
                    "frontend_repos": ["acme-eng/widget-frontend"],
                    "path": str(overlay_path),
                }
            },
        )
        monkeypatch.setenv("T3_CONFIG_DB", str(db))
        _empty_home(tmp_path, monkeypatch)

        slugs, paths = managed_repo.overlay_managed_repo_signals()

        assert "acme-eng/widget-overlay" in slugs
        assert "acme-eng/widget-frontend" in slugs
        assert "souliane/teatree" in slugs
        assert overlay_path.resolve() in paths


class TestSelfDmDestinationIdsDbFirst:
    def test_overlay_slack_ids_from_db(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        db = tmp_path / "config.sqlite3"
        _make_config_db(
            db,
            overlays={
                "t3-acme": {"slack_user_id": "U0ACMEUSER0", "slack_dm_channel_id": "D0DEMOCHAN1"},
                "t3-widget": {"slack_dm_channel_id": "D0DEMOCHAN2"},
            },
        )
        monkeypatch.setenv("T3_CONFIG_DB", str(db))
        _empty_home(tmp_path, monkeypatch)

        dest = router._self_dm_destination_ids()

        assert dest.resolved is True
        assert "U0ACMEUSER0" in dest.ids
        assert "D0DEMOCHAN1" in dest.ids
        assert "D0DEMOCHAN2" in dest.ids

    def test_unresolved_when_no_db(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _empty_home(tmp_path, monkeypatch)
        _no_db(tmp_path, monkeypatch)

        dest = router._self_dm_destination_ids()

        assert dest.resolved is False
        assert dest.ids == frozenset()
