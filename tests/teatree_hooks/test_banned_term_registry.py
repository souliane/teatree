"""Tests for the consolidated, class-tagged banned-term registry (registry PR 1).

The registry collapses the three legacy sources (``banned_terms`` →
``prose_collider``, ``banned_brands`` → ``leak``, ``banned_terms_allowlist`` →
``allow``) into one class-tagged row, and :func:`terms_for_gate` routes each
gate to the classes it scans. The load-bearing PR-1 contracts pinned here:

* **dual-read = no behaviour change** — with the registry UNSET (today's state)
    ``terms_for_gate`` returns EXACTLY the old per-source config;
* **fail-closed** — with BOTH the registry and the legacy source unset, the gate
    REFUSES (raises), never scans as empty;
* **class routing** — a ``leak`` term is scanned by diff+tree+core, a ``tone``
    term only by diff;
* a set-but-malformed registry fails CLOSED, never silently empty.

All terms are SYNTHETIC (``acme`` / ``widget-margin`` / ``democorp``) — no real
customer value, so this public test leaks nothing.
"""

import json
import sqlite3
from pathlib import Path

import pytest

from teatree.config.registries import COLD_SETTINGS
from teatree.config.secret_settings import SECRET_SETTINGS
from teatree.hooks import banned_term_registry
from teatree.hooks.banned_term_registry import allowlist_terms, load_registry, registry_terms_for_gate, terms_for_gate
from teatree.hooks.banned_terms_cli import resolve_banned_terms
from teatree.hooks.banned_terms_tree_scan import BannedTermsUnsetError, load_brand_terms


@pytest.fixture(autouse=True)
def _no_ambient_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop every ambient term/brand/registry env so the DB is the only source."""
    for name in ("T3_BANNED_TERMS", "TEATREE_BANNED_BRANDS", "TEATREE_TERM_REGISTRY"):
        monkeypatch.delenv(name, raising=False)


def _seed(tmp_path: Path, **rows: object) -> Path:
    """Write a config DB with a ``teatree_config_setting`` row per keyword arg (JSON-encoded)."""
    db = tmp_path / "config.sqlite3"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS teatree_config_setting ("
        "id INTEGER PRIMARY KEY, scope TEXT NOT NULL DEFAULT '', key TEXT NOT NULL, value TEXT NOT NULL)"
    )
    for key, value in rows.items():
        conn.execute(
            "INSERT INTO teatree_config_setting (scope, key, value) VALUES ('', ?, ?)",
            (key, json.dumps(value)),
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


class TestDualReadFallbackIsNoBehaviourChange:
    """Registry UNSET ⇒ terms_for_gate returns EXACTLY the old per-source config."""

    def test_diff_and_core_fall_back_to_legacy_banned_terms(self, tmp_path: Path) -> None:
        db = _seed(tmp_path, banned_terms=["acme", "widget-margin"])
        assert terms_for_gate("diff", db_path=db) == ("acme", "widget-margin")
        assert terms_for_gate("core", db_path=db) == ("acme", "widget-margin")

    def test_tree_falls_back_to_legacy_banned_brands(self, tmp_path: Path) -> None:
        db = _seed(tmp_path, banned_terms=["acme"], banned_brands=["democorp", "globex"])
        assert terms_for_gate("tree", db_path=db) == ("democorp", "globex")

    def test_allow_falls_back_to_legacy_allowlist(self, tmp_path: Path) -> None:
        db = _seed(tmp_path, banned_terms=["acme"], banned_terms_allowlist=["myorg-product"])
        assert terms_for_gate("allow", db_path=db) == ("myorg-product",)

    def test_legacy_resolvers_unchanged_when_registry_unset(self, tmp_path: Path) -> None:
        db = _seed(tmp_path, banned_terms=["acme"], banned_brands=["democorp"], banned_terms_allowlist=["myorg"])
        assert load_registry(db_path=db) is None
        assert resolve_banned_terms(db_path=db) == ("acme",)
        assert load_brand_terms(db_path=db) == ("democorp",)
        assert allowlist_terms(db) == ("myorg",)


class TestBothUnsetRefusesFailClosed:
    """BOTH the registry and the legacy source unset ⇒ REFUSE (raise), never empty."""

    def test_diff_gate_refuses_when_registry_and_banned_terms_both_unset(self, tmp_path: Path) -> None:
        with pytest.raises(BannedTermsUnsetError):
            terms_for_gate("diff", db_path=_empty_db(tmp_path))

    def test_core_gate_refuses_when_both_unset(self, tmp_path: Path) -> None:
        with pytest.raises(BannedTermsUnsetError):
            terms_for_gate("core", db_path=_empty_db(tmp_path))

    def test_tree_gate_refuses_when_registry_and_banned_brands_both_unset(self, tmp_path: Path) -> None:
        # banned_terms is set but banned_brands is not: the tree gate's own source
        # is unset, so it must still REFUSE rather than scan an empty brand list.
        db = _seed(tmp_path, banned_terms=["acme"])
        with pytest.raises(BannedTermsUnsetError):
            terms_for_gate("tree", db_path=db)

    def test_missing_db_refuses(self, tmp_path: Path) -> None:
        with pytest.raises(BannedTermsUnsetError):
            terms_for_gate("diff", db_path=tmp_path / "absent.sqlite3")


class TestClassRouting:
    """A leak term is scanned by diff+tree+core; a tone term only by diff."""

    @pytest.fixture
    def registry_db(self, tmp_path: Path) -> Path:
        return _seed(
            tmp_path,
            banned_term_registry={
                "leak": ["democorp"],
                "prose_collider": ["widget-margin"],
                "tone": ["synergy"],
                "allow": ["myorg-product"],
            },
        )

    def test_leak_term_is_scanned_by_diff_tree_and_core(self, registry_db: Path) -> None:
        assert "democorp" in terms_for_gate("diff", db_path=registry_db)
        assert "democorp" in terms_for_gate("tree", db_path=registry_db)
        assert "democorp" in terms_for_gate("core", db_path=registry_db)

    def test_tone_term_is_scanned_only_by_diff(self, registry_db: Path) -> None:
        assert "synergy" in terms_for_gate("diff", db_path=registry_db)
        assert "synergy" not in terms_for_gate("tree", db_path=registry_db)
        assert "synergy" not in terms_for_gate("core", db_path=registry_db)

    def test_prose_collider_term_is_scanned_by_diff_and_core_not_tree(self, registry_db: Path) -> None:
        assert "widget-margin" in terms_for_gate("diff", db_path=registry_db)
        assert "widget-margin" in terms_for_gate("core", db_path=registry_db)
        assert "widget-margin" not in terms_for_gate("tree", db_path=registry_db)

    def test_allow_class_routes_to_the_allowlist(self, registry_db: Path) -> None:
        assert terms_for_gate("allow", db_path=registry_db) == ("myorg-product",)
        assert allowlist_terms(registry_db) == ("myorg-product",)

    def test_tree_gate_sees_exactly_the_leak_class(self, registry_db: Path) -> None:
        assert terms_for_gate("tree", db_path=registry_db) == ("democorp",)

    def test_registry_wins_over_legacy_rows_when_present(self, tmp_path: Path) -> None:
        # A present registry is authoritative: the legacy rows are NOT unioned in.
        db = _seed(
            tmp_path,
            banned_terms=["legacy-term"],
            banned_brands=["legacy-brand"],
            banned_term_registry={"leak": ["democorp"], "prose_collider": ["widget-margin"]},
        )
        assert terms_for_gate("diff", db_path=db) == ("democorp", "widget-margin")
        assert "legacy-term" not in terms_for_gate("diff", db_path=db)
        assert terms_for_gate("tree", db_path=db) == ("democorp",)


class TestLegacyResolversDualRead:
    """The legacy public resolvers dual-read the registry too (registry-first)."""

    def test_resolve_banned_terms_reads_registry_diff_classes(self, tmp_path: Path) -> None:
        db = _seed(tmp_path, banned_term_registry={"leak": ["democorp"], "prose_collider": ["widget-margin"]})
        assert resolve_banned_terms(db_path=db) == ("democorp", "widget-margin")

    def test_load_brand_terms_reads_registry_leak_class(self, tmp_path: Path) -> None:
        db = _seed(tmp_path, banned_term_registry={"leak": ["democorp"], "prose_collider": ["widget-margin"]})
        assert load_brand_terms(db_path=db) == ("democorp",)

    def test_env_override_still_wins_over_registry_for_resolve(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The CI-secret env path stays authoritative through the transition.
        db = _seed(tmp_path, banned_term_registry={"leak": ["democorp"]})
        monkeypatch.setenv("T3_BANNED_TERMS", "acme")
        assert resolve_banned_terms(db_path=db) == ("acme",)


class TestMalformedRegistryFailsClosed:
    """A set-but-malformed registry REFUSES, never scans as empty."""

    def test_registry_row_not_a_table_raises(self, tmp_path: Path) -> None:
        db = _seed(tmp_path, banned_term_registry=["not", "a", "table"])
        with pytest.raises(BannedTermsUnsetError):
            terms_for_gate("diff", db_path=db)

    def test_registry_class_value_not_a_list_raises(self, tmp_path: Path) -> None:
        db = _seed(tmp_path, banned_term_registry={"leak": "democorp"})
        with pytest.raises(BannedTermsUnsetError):
            terms_for_gate("diff", db_path=db)

    def test_registry_env_invalid_json_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEATREE_TERM_REGISTRY", "{not json")
        with pytest.raises(BannedTermsUnsetError):
            terms_for_gate("diff", db_path=_empty_db(tmp_path))


class TestRegistryEnvPath:
    """The $TEATREE_TERM_REGISTRY secret feeds the registry without a DB row."""

    def test_env_registry_routes_by_class(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(
            "TEATREE_TERM_REGISTRY",
            json.dumps({"leak": ["democorp"], "tone": ["synergy"]}),
        )
        db = _empty_db(tmp_path)
        assert terms_for_gate("tree", db_path=db) == ("democorp",)
        assert "synergy" in terms_for_gate("diff", db_path=db)
        assert "synergy" not in terms_for_gate("tree", db_path=db)


class TestRegistryOnlyResolver:
    """registry_terms_for_gate is the registry-only half of the dual-read."""

    def test_returns_none_when_registry_unset(self, tmp_path: Path) -> None:
        db = _seed(tmp_path, banned_terms=["acme"])  # legacy row, no registry
        assert registry_terms_for_gate("diff", db_path=db) is None

    def test_returns_registry_classes_when_set(self, tmp_path: Path) -> None:
        db = _seed(tmp_path, banned_term_registry={"leak": ["democorp"], "prose_collider": ["widget-margin"]})
        assert registry_terms_for_gate("tree", db_path=db) == ("democorp",)
        assert registry_terms_for_gate("diff", db_path=db) == ("democorp", "widget-margin")


class TestRegistryTolerance:
    """Unknown top-level keys are ignored; an unknown gate name is a hard error."""

    def test_unknown_top_level_key_is_ignored(self, tmp_path: Path) -> None:
        db = _seed(tmp_path, banned_term_registry={"leak": ["democorp"], "future_class": ["ignored"]})
        assert terms_for_gate("tree", db_path=db) == ("democorp",)
        assert "ignored" not in terms_for_gate("diff", db_path=db)

    def test_unknown_gate_in_legacy_fallback_raises(self, tmp_path: Path) -> None:
        # Registry unset ⇒ terms_for_gate routes to the legacy dispatch, which
        # rejects an unknown gate name.
        with pytest.raises(ValueError, match="unknown banned-terms gate"):
            terms_for_gate("bogus", db_path=_seed(tmp_path, banned_terms=["acme"]))


def test_registry_key_is_a_registered_settable_secret() -> None:
    # The consolidated key is settable via `config_setting set` (validated as a
    # table) and withheld from a shared export (it carries brand codenames).
    assert "banned_term_registry" in COLD_SETTINGS
    assert "banned_term_registry" in SECRET_SETTINGS


def test_unknown_gate_name_is_a_value_error() -> None:
    with pytest.raises(ValueError, match="unknown banned-terms gate"):
        banned_term_registry._classes_union({"leak": (), "prose_collider": (), "tone": (), "allow": ()}, "bogus")
