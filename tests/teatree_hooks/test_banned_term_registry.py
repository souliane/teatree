"""The classed registry is the sole source for every banned-term gate."""

import json
import sqlite3
from pathlib import Path

import pytest

from teatree.config.registries import COLD_SETTINGS
from teatree.config.secret_settings import SECRET_SETTINGS
from teatree.hooks.banned_term_registry import allowlist_terms, export_scan_terms, terms_for_gate
from teatree.hooks.banned_terms_cli import resolve_banned_terms
from teatree.hooks.banned_terms_tree_scan import BannedTermsUnsetError, load_brand_terms


@pytest.fixture(autouse=True)
def _no_registry_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEATREE_TERM_REGISTRY", raising=False)


def _seed(tmp_path: Path, **rows: object) -> Path:
    db = tmp_path / "config.sqlite3"
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "CREATE TABLE teatree_config_setting ("
            "id INTEGER PRIMARY KEY, scope TEXT NOT NULL DEFAULT '', key TEXT NOT NULL, value TEXT NOT NULL)"
        )
        for key, value in rows.items():
            conn.execute(
                "INSERT INTO teatree_config_setting (scope, key, value) VALUES ('', ?, ?)",
                (key, json.dumps(value)),
            )
        conn.commit()
    finally:
        conn.close()
    return db


def test_registry_routes_classes_to_their_gates(tmp_path: Path) -> None:
    db = _seed(
        tmp_path,
        banned_term_registry={
            "leak": ["democorp"],
            "prose_collider": ["green-gizmo"],
            "tone": ["synergy"],
            "overlay": ["private-code"],
            "allow": ["myorg-product"],
        },
    )
    assert terms_for_gate("diff", db_path=db) == ("democorp", "green-gizmo", "synergy")
    assert terms_for_gate("core", db_path=db) == ("democorp", "green-gizmo")
    assert terms_for_gate("tree", db_path=db) == ("democorp",)
    assert terms_for_gate("overlay", db_path=db) == ("private-code",)
    assert allowlist_terms(db) == ("myorg-product",)
    assert set(export_scan_terms(db_path=db)) == {"democorp", "green-gizmo", "synergy", "private-code"}
    assert resolve_banned_terms(db_path=db) == terms_for_gate("diff", db_path=db)
    assert load_brand_terms(db_path=db) == terms_for_gate("tree", db_path=db)


def test_legacy_rows_do_not_supply_a_fallback(tmp_path: Path) -> None:
    db = _seed(tmp_path, banned_terms=["old"], banned_brands=["old"])
    with pytest.raises(BannedTermsUnsetError):
        terms_for_gate("diff", db_path=db)
    with pytest.raises(BannedTermsUnsetError):
        terms_for_gate("tree", db_path=db)
    assert terms_for_gate("overlay", db_path=db) == ()


def test_registry_secret_wins_over_row(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = _seed(tmp_path, banned_term_registry={"leak": ["old"], "prose_collider": []})
    monkeypatch.setenv("TEATREE_TERM_REGISTRY", json.dumps({"leak": ["new"], "prose_collider": []}))
    assert terms_for_gate("tree", db_path=db) == ("new",)


@pytest.mark.parametrize("registry", [[], {"leak": "bad", "prose_collider": []}])
def test_malformed_registry_fails_loud(tmp_path: Path, registry: object) -> None:
    db = _seed(tmp_path, banned_term_registry=registry)
    with pytest.raises(BannedTermsUnsetError):
        terms_for_gate("diff", db_path=db)


def test_a_registry_holding_only_leak_terms_feeds_the_diff_gate(tmp_path: Path) -> None:
    db = _seed(tmp_path, banned_term_registry={"leak": ["acme"]})
    assert terms_for_gate("diff", db_path=db) == ("acme",)


def test_empty_critical_scan_classes_refuse_even_when_registry_exists(tmp_path: Path) -> None:
    db = _seed(tmp_path, banned_term_registry={"allow": ["myorg"]})
    for gate in ("diff", "core", "tree"):
        with pytest.raises(BannedTermsUnsetError, match=f"no terms for the {gate} scan"):
            terms_for_gate(gate, db_path=db)


def test_registry_is_settable_and_private() -> None:
    assert "banned_term_registry" in COLD_SETTINGS
    assert "banned_term_registry" in SECRET_SETTINGS
    assert not {"banned_terms", "banned_brands", "banned_terms_allowlist", "overlay_leak_terms"} & COLD_SETTINGS.keys()
