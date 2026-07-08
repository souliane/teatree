"""Tests for the shared banned-terms source resolver (config-unify, task #36).

``resolve_banned_terms`` is the single source-resolution every banned-terms
scanner shares so they cannot diverge on WHERE the term list comes from. The
list is DB-home: ``T3_BANNED_TERMS`` env override → the ``banned_terms``
``ConfigSetting`` row (read Django-free via ``teatree.config.cold_reader``). A
genuinely-unset list RAISES rather than silently degrading to an empty ban list
— the anti-vacuity contract that keeps the security gate from going inert on a
load bug.

All terms here are SYNTHETIC (``acme`` / ``widget-margin``) — no real customer
value, so this public test leaks nothing.
"""

import json
import sqlite3
from pathlib import Path

import pytest

from teatree.hooks.banned_terms_cli import resolve_banned_terms
from teatree.hooks.banned_terms_tree_scan import BannedTermsUnsetError

_SYNTHETIC_TERMS = ("acme", "widget-margin")


@pytest.fixture(autouse=True)
def _no_ambient_terms_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop any ambient ``T3_BANNED_TERMS`` so the DB is the only source under test."""
    monkeypatch.delenv("T3_BANNED_TERMS", raising=False)


def _seed(tmp_path: Path, terms: list[str]) -> Path:
    db = tmp_path / "config.sqlite3"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS teatree_config_setting ("
        "id INTEGER PRIMARY KEY, scope TEXT NOT NULL DEFAULT '', key TEXT NOT NULL, value TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO teatree_config_setting (scope, key, value) VALUES ('', 'banned_terms', ?)",
        (json.dumps(terms),),
    )
    conn.commit()
    conn.close()
    return db


def _empty_db(tmp_path: Path) -> Path:
    db = tmp_path / "empty.sqlite3"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE teatree_config_setting (id INTEGER PRIMARY KEY, scope TEXT, key TEXT, value TEXT)")
    conn.commit()
    conn.close()
    return db


class TestResolveBannedTerms:
    def test_db_list_is_honoured(self, tmp_path: Path) -> None:
        db = _seed(tmp_path, ["acme", "widget-margin"])
        assert resolve_banned_terms(db_path=db) == _SYNTHETIC_TERMS

    def test_env_override_wins_over_db(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        db = _seed(tmp_path, ["from-db"])
        monkeypatch.setenv("T3_BANNED_TERMS", "acme, widget-margin")
        assert resolve_banned_terms(db_path=db) == _SYNTHETIC_TERMS

    def test_env_value_arg_wins_over_db(self, tmp_path: Path) -> None:
        db = _seed(tmp_path, ["from-db"])
        assert resolve_banned_terms(db_path=db, env_value="acme, widget-margin") == _SYNTHETIC_TERMS

    def test_env_override_without_a_db(self, tmp_path: Path) -> None:
        assert resolve_banned_terms(db_path=tmp_path / "absent.sqlite3", env_value="acme,widget-margin") == (
            _SYNTHETIC_TERMS
        )

    def test_missing_db_raises_rather_than_silently_empty(self, tmp_path: Path) -> None:
        # No env AND no DB row is genuinely UNSET → fail loud, never empty.
        with pytest.raises(BannedTermsUnsetError):
            resolve_banned_terms(db_path=tmp_path / "absent.sqlite3")

    def test_explicit_empty_list_is_a_deliberate_no_op(self, tmp_path: Path) -> None:
        db = _seed(tmp_path, [])
        assert resolve_banned_terms(db_path=db) == ()

    def test_unset_row_raises_rather_than_silently_empty(self, tmp_path: Path) -> None:
        # The anti-vacuity contract: a DB with no ``banned_terms`` row is a
        # load-bug-shaped UNSET, not a deliberate no-terms choice, so it must
        # RAISE — never degrade to an empty ban list that disables the gate.
        with pytest.raises(BannedTermsUnsetError):
            resolve_banned_terms(db_path=_empty_db(tmp_path))

    def test_blank_env_value_falls_through_to_db(self, tmp_path: Path) -> None:
        db = _seed(tmp_path, ["acme"])
        assert resolve_banned_terms(db_path=db, env_value="   ") == ("acme",)

    def test_legacy_config_path_arg_is_ignored(self, tmp_path: Path) -> None:
        # The pre-DB caller (scripts/privacy_scan) still passes a config path
        # positionally; it is accepted and NOT consulted — the list is DB-home.
        db = _seed(tmp_path, ["acme"])
        assert resolve_banned_terms(tmp_path / "legacy.toml", db_path=db) == ("acme",)
