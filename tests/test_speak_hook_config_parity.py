"""Hook-side ``_speak_settings`` and config-side ``speak_from_subtable`` agree (#2060).

``speak`` is DB-home (legacy file tier removed): the Stop hook reads the stored
``speak`` JSON dict from the canonical sqlite via the Django-free ``cold_reader``
(it cannot cheaply import the Django config), so it carries a small pure-Python
duplicate of the sub-table interpretation. This golden-corpus parity test pins the
two to the same ``(local, slack)`` for every stored shape, so the duplicate can
never drift from :func:`speak_from_subtable` (the config source of truth).
"""

import json
import sqlite3
from pathlib import Path

import pytest

import hooks.scripts.hook_router as router
from teatree.config_speak import resolve_speak, speak_from_subtable
from teatree.types import SpeakConfig

_CORPUS: list[dict[str, object] | None] = [
    {"local": "all", "slack": True},
    {"slack": True},
    {"local": "all"},
    {"local": "dm"},
    {"local": "off"},
    {},
    None,
]


def _seed_speak_db(db: Path, value: object | None) -> None:
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "CREATE TABLE teatree_config_setting ("
            "id INTEGER PRIMARY KEY, scope TEXT NOT NULL DEFAULT '', "
            "key TEXT NOT NULL, value TEXT NOT NULL)"
        )
        if value is not None:
            conn.execute(
                "INSERT INTO teatree_config_setting (scope, key, value) VALUES ('', 'speak', ?)",
                (json.dumps(value),),
            )
        conn.commit()
    finally:
        conn.close()


@pytest.mark.parametrize("stored", _CORPUS)
def test_hook_and_config_dict_parity(
    stored: dict[str, object] | None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "db.sqlite3"
    _seed_speak_db(db, stored)
    monkeypatch.setenv("T3_CONFIG_DB", str(db))

    hook_local, hook_slack = router._speak_settings()
    expected = SpeakConfig() if stored is None else speak_from_subtable(stored)

    assert (hook_local, hook_slack) == (expected.local.value, expected.slack)


def test_resolve_speak_is_the_config_source_of_truth() -> None:
    # The hook map mirrors the speak sub-table builder; pin that the config helper
    # exists and reads the same sub-table shape the hook duplicates.
    assert resolve_speak({"speak": {"local": "all"}}).local.value == "all"
