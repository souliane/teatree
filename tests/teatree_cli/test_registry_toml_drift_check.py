# test-path: cross-cutting
"""`t3 doctor` hard-FAILs on a DB-home registry table masked by a diverging DB row (#128).

The ``overlays`` / ``e2e_repos`` registries are DB-home (#1775); a lingering
``[overlays]`` / ``[e2e_repos]`` table in ~/.teatree.toml whose value diverges from the
authoritative ``ConfigSetting`` row is silently ignored on read (a stale worktree ``path``
returned with zero signal). The doctor check surfaces it LOUD with the reconcile command.
"""

import io
import json
import sqlite3
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from teatree.cli._doctor_checks import _check_registry_toml_drift


def _seed_registry_db(path: Path, **rows: object) -> None:
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


def _run(config_path: Path) -> tuple[bool, str]:
    out = io.StringIO()
    with redirect_stdout(out):
        ok = _check_registry_toml_drift(config_path=config_path)
    return ok, out.getvalue()


def test_fails_loud_on_masked_overlay_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = tmp_path / ".teatree.toml"
    config_path.write_text('[overlays.myoverlay]\npath = "~/workspace/canonical"\n', encoding="utf-8")
    db = tmp_path / "db.sqlite3"
    _seed_registry_db(db, overlays={"myoverlay": {"path": "~/workspace/stale-worktree"}})
    monkeypatch.setenv("T3_CONFIG_DB", str(db))

    ok, message = _run(config_path)

    assert ok is False
    assert "FAIL" in message
    assert "myoverlay" in message
    assert "config_setting import" in message


def test_passes_when_file_agrees_with_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = tmp_path / ".teatree.toml"
    config_path.write_text('[overlays.myoverlay]\npath = "~/workspace/same"\n', encoding="utf-8")
    db = tmp_path / "db.sqlite3"
    _seed_registry_db(db, overlays={"myoverlay": {"path": "~/workspace/same"}})
    monkeypatch.setenv("T3_CONFIG_DB", str(db))

    ok, message = _run(config_path)

    assert ok is True
    assert "FAIL" not in message


def test_passes_when_config_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("T3_CONFIG_DB", str(tmp_path / "absent.sqlite3"))

    ok, message = _run(tmp_path / "absent.toml")

    assert ok is True
    assert "FAIL" not in message
