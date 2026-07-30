"""Tests for the self-verifying ``t3 banned-terms migrate-registry`` command.

The migration reads the current ``banned_terms`` + ``banned_brands`` + allowlist
and class-tags them into the consolidated ``banned_term_registry``, then
SELF-VERIFIES the result reproduces every effective term the old config yields so
the cutover (PR 2) is provably lossless. The load-bearing contracts pinned here:

* the produced registry reproduces the total effective term set (no term dropped);
* the self-verify FAILS LOUD when a migration would drop a term;
* the CLI exits 0 (prints the JSON to set) on a lossless migration and 2 on a
    lossy one.

All terms are SYNTHETIC — no real customer value, so this public test leaks nothing.
"""

import json
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from teatree.cli.banned_terms import banned_terms_app
from teatree.core.banned_terms_tree import BannedTermsUnsetError, migrate_registry, scan_committed_tree
from teatree.hooks import banned_term_registry
from teatree.hooks.banned_term_registry import build_registry_from_legacy, verify_migration

_runner = CliRunner()


@pytest.fixture(autouse=True)
def _no_ambient_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("T3_BANNED_TERMS", "TEATREE_BANNED_BRANDS", "TEATREE_TERM_REGISTRY", "T3_CONFIG_DB"):
        monkeypatch.delenv(name, raising=False)


def _seed(tmp_path: Path, **rows: object) -> Path:
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


class TestMigrationIsLossless:
    def test_registry_reproduces_every_effective_term(self, tmp_path: Path) -> None:
        db = _seed(
            tmp_path,
            banned_terms=["acme", "widget-margin"],
            banned_brands=["democorp", "globex"],
            banned_terms_allowlist=["myorg-product"],
        )
        result = migrate_registry(config_path=db)
        assert result.verification.ok
        assert set(result.registry["leak"]) == {"democorp", "globex"}
        assert set(result.registry["prose_collider"]) == {"acme", "widget-margin"}
        assert result.registry["tone"] == []
        assert set(result.registry["allow"]) == {"myorg-product"}

    def test_produced_registry_preserves_each_gate(self, tmp_path: Path) -> None:
        db = _seed(tmp_path, banned_terms=["acme"], banned_brands=["democorp"], banned_terms_allowlist=["myorg"])
        registry = build_registry_from_legacy(db_path=db)
        # The high-confidence brand must stay in the tree gate; the flat term must
        # stay in diff+core; neither gate loses a term it scanned before.
        assert "democorp" in banned_term_registry._classes_union(_normalise(registry), "tree")
        assert "acme" in banned_term_registry._classes_union(_normalise(registry), "diff")
        assert "acme" in banned_term_registry._classes_union(_normalise(registry), "core")

    def test_lossless_when_brands_are_unset(self, tmp_path: Path) -> None:
        # A no-brands operator: banned_brands unset ⇒ empty leak, still lossless.
        db = _seed(tmp_path, banned_terms=["acme"])
        result = migrate_registry(config_path=db)
        assert result.verification.ok
        assert result.registry["leak"] == []
        assert result.registry["prose_collider"] == ["acme"]


class TestSelfVerifyFailsLoudOnDrop:
    def test_dropped_prose_collider_term_is_caught(self, tmp_path: Path) -> None:
        db = _seed(tmp_path, banned_terms=["acme", "widget-margin"], banned_brands=["democorp"])
        good = build_registry_from_legacy(db_path=db)
        lossy = {**good, "prose_collider": good["prose_collider"][:-1]}
        verification = verify_migration(lossy, db_path=db)
        assert not verification.ok
        assert verification.dropped  # the dropped term is named
        assert "would stop scanning" in verification.failure_reason()

    def test_dropped_leak_term_is_caught(self, tmp_path: Path) -> None:
        db = _seed(tmp_path, banned_terms=["acme"], banned_brands=["democorp", "globex"])
        good = build_registry_from_legacy(db_path=db)
        lossy = {**good, "leak": ["democorp"]}  # drop globex
        verification = verify_migration(lossy, db_path=db)
        assert not verification.ok
        assert "globex" in verification.dropped
        assert "tree" in verification.per_gate_drops

    def test_allowlist_mismatch_is_caught(self, tmp_path: Path) -> None:
        db = _seed(tmp_path, banned_terms=["acme"], banned_terms_allowlist=["myorg-product"])
        good = build_registry_from_legacy(db_path=db)
        lossy = {**good, "allow": []}  # drop the allowlist entry
        verification = verify_migration(lossy, db_path=db)
        assert not verification.ok
        assert verification.allow_mismatch
        assert "allow class" in verification.failure_reason()

    def test_fabricated_term_is_caught(self, tmp_path: Path) -> None:
        db = _seed(tmp_path, banned_terms=["acme"])
        good = build_registry_from_legacy(db_path=db)
        lossy = {**good, "prose_collider": [*good["prose_collider"], "fabricated"]}
        verification = verify_migration(lossy, db_path=db)
        assert not verification.ok
        assert "fabricated" in verification.added
        assert "unexpected terms" in verification.failure_reason()

    def test_lossless_verification_has_no_failure_reason(self, tmp_path: Path) -> None:
        db = _seed(tmp_path, banned_terms=["acme"], banned_brands=["democorp"])
        verification = migrate_registry(config_path=db).verification
        assert verification.ok
        assert verification.failure_reason() == ""


class TestScanTreeAllowUnset:
    """--allow-unset opts a genuinely-unset brand list into the terminology-only pass."""

    def test_unset_brands_without_allow_unset_raises(self, tmp_path: Path) -> None:
        db = _seed(tmp_path, banned_terms=["acme"])  # no banned_brands row
        with pytest.raises(BannedTermsUnsetError):
            scan_committed_tree(tmp_path, config_path=db, allow_unset=False)

    def test_unset_brands_with_allow_unset_is_inert(self, tmp_path: Path) -> None:
        db = _seed(tmp_path, banned_terms=["acme"])  # no banned_brands row
        result = scan_committed_tree(tmp_path, config_path=db, allow_unset=True)
        assert result.brands_configured is False  # INERT, not a raise

    def test_cli_scan_tree_allow_unset_exits_zero_on_unset_brands(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db = _seed(tmp_path, banned_terms=["acme"])  # no banned_brands row
        monkeypatch.setenv("T3_CONFIG_DB", str(db))
        result = _runner.invoke(banned_terms_app, ["scan-tree", "--repo-root", str(tmp_path), "--allow-unset"])
        assert result.exit_code == 0

    def test_cli_scan_tree_without_allow_unset_exits_two_on_unset_brands(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db = _seed(tmp_path, banned_terms=["acme"])  # no banned_brands row
        monkeypatch.setenv("T3_CONFIG_DB", str(db))
        result = _runner.invoke(banned_terms_app, ["scan-tree", "--repo-root", str(tmp_path)])
        assert result.exit_code == 2


class TestMigrateRegistryCli:
    def test_lossless_exits_zero_and_prints_settable_json(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db = _seed(tmp_path, banned_terms=["acme"], banned_brands=["democorp"], banned_terms_allowlist=["myorg"])
        monkeypatch.setenv("T3_CONFIG_DB", str(db))
        result = _runner.invoke(banned_terms_app, ["migrate-registry"])
        assert result.exit_code == 0
        # The JSON registry the operator sets at cutover is emitted.
        start = result.stdout.index("{")
        payload = json.loads(result.stdout[start : result.stdout.rindex("}") + 1])
        assert payload["leak"] == ["democorp"]
        assert payload["prose_collider"] == ["acme"]

    def test_lossy_migration_exits_two(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        db = _seed(tmp_path, banned_terms=["acme", "widget-margin"], banned_brands=["democorp"])
        monkeypatch.setenv("T3_CONFIG_DB", str(db))
        real_build = banned_term_registry.build_registry_from_legacy

        def _lossy(db_path: Path | None = None) -> dict[str, list[str]]:
            built = real_build(db_path=db_path)
            built["prose_collider"] = built["prose_collider"][:-1]  # silently DROP a term
            return built

        monkeypatch.setattr(banned_term_registry, "build_registry_from_legacy", _lossy)
        result = _runner.invoke(banned_terms_app, ["migrate-registry"])
        assert result.exit_code == 2
        assert "LOSSY" in result.stdout


def _normalise(registry: dict[str, list[str]]) -> dict[str, tuple[str, ...]]:
    return banned_term_registry._normalise_registry(registry)
