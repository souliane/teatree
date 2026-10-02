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

from teatree.hooks.banned_terms_cli import (
    UnsetVerdict,
    _diff_only_report,
    _full_file_report,
    banned_terms_required,
    main,
    report_unset,
    resolve_banned_terms,
    resolve_unset_verdict,
)
from teatree.hooks.banned_terms_tree_scan import BannedTermsUnreadableError, BannedTermsUnsetError

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


def _seed_setting(tmp_path: Path, key: str, *, value: object, name: str = "config.sqlite3") -> Path:
    db = tmp_path / name
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS teatree_config_setting ("
        "id INTEGER PRIMARY KEY, scope TEXT NOT NULL DEFAULT '', key TEXT NOT NULL, value TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO teatree_config_setting (scope, key, value) VALUES ('', ?, ?)",
        (key, json.dumps(value)),
    )
    conn.commit()
    conn.close()
    return db


@pytest.fixture(autouse=True)
def _no_ambient_required_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop any ambient ``T3_BANNED_TERMS_REQUIRED`` so the DB/default is under test."""
    monkeypatch.delenv("T3_BANNED_TERMS_REQUIRED", raising=False)


class TestBannedTermsRequired:
    """An UNSET list warns-and-allows by default; ``banned_terms_required`` restores fail-closed (#3247)."""

    def test_default_is_false_on_empty_db(self, tmp_path: Path) -> None:
        assert banned_terms_required(db_path=_empty_db(tmp_path)) is False

    def test_db_true_makes_it_required(self, tmp_path: Path) -> None:
        db = _seed_setting(tmp_path, "banned_terms_required", value=True)
        assert banned_terms_required(db_path=db) is True

    def test_env_override_wins_over_db(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        db = _seed_setting(tmp_path, "banned_terms_required", value=False)
        monkeypatch.setenv("T3_BANNED_TERMS_REQUIRED", "1")
        assert banned_terms_required(db_path=db) is True

    @pytest.mark.parametrize("raw", ["1", "true", "yes", "on", "TRUE", "Yes"])
    def test_env_truthy_variants(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
        monkeypatch.setenv("T3_BANNED_TERMS_REQUIRED", raw)
        assert banned_terms_required(db_path=_empty_db(tmp_path)) is True

    def test_env_falsey_is_not_required(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_BANNED_TERMS_REQUIRED", "0")
        assert banned_terms_required(db_path=_empty_db(tmp_path)) is False


class TestReportUnset:
    """The unset disposition: warn-and-allow (exit 0) by default, fail-closed (exit 2) when required (#3247)."""

    def _exc(self) -> BannedTermsUnsetError:
        return BannedTermsUnsetError.for_key("banned_terms", "T3_BANNED_TERMS")

    def test_not_required_warns_and_allows(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        code = report_unset(self._exc(), db_path=_empty_db(tmp_path))
        assert code == 0
        err = capsys.readouterr().err
        assert "WARNING" in err
        assert "UNSET" in err
        assert "banned_terms_required" in err

    def test_required_fails_closed_exit_2(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        db = _seed_setting(tmp_path, "banned_terms_required", value=True)
        code = report_unset(self._exc(), db_path=db)
        assert code == 2
        assert "banned_terms is unset" in capsys.readouterr().err


class TestMainUnsetDisposition:
    """End-to-end through ``main``: an unset list allows a clean diff by default (#3247 acceptance)."""

    def test_unset_clean_diff_proceeds_exit_0(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.delenv("T3_BANNED_TERMS", raising=False)
        monkeypatch.setenv("T3_CONFIG_DB", str(_empty_db(tmp_path)))
        assert main([]) == 0
        assert "WARNING" in capsys.readouterr().err

    def test_unset_required_fails_closed_exit_2(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_BANNED_TERMS", raising=False)
        monkeypatch.setenv("T3_CONFIG_DB", str(_seed_setting(tmp_path, "banned_terms_required", value=True)))
        assert main([]) == 2

    def test_configured_list_still_blocks_a_real_term(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # A CONFIGURED non-empty list still hard-blocks a real banned term (safety
        # net intact) — only the UNSET case changed.
        monkeypatch.delenv("T3_BANNED_TERMS", raising=False)
        monkeypatch.setenv("T3_CONFIG_DB", str(_seed(tmp_path, ["acme"])))
        offender = tmp_path / "leak.txt"
        offender.write_text("the acme deal closes friday\n", encoding="utf-8")
        assert main([str(offender)]) == 1


def _corrupt_db(tmp_path: Path) -> Path:
    """A store that EXISTS but every read of it errors — a locked/corrupt DB's signature."""
    db = tmp_path / "corrupt.sqlite3"
    db.write_bytes(b"this is not a sqlite database")
    return db


class TestUnreadableStoreFailsClosed:
    """A store that could not be READ is not an unset list — it fails CLOSED regardless (#4008).

    The field report: on a box where ``banned_terms`` IS configured, one invocation printed the
    unset warning and exited 0 while the invocations either side of it read the full list and
    correctly exited 1. A read that errors is indistinguishable from an absent row, so the
    warn-and-allow disposition (#3247) opened the gate on a transient DB failure.
    """

    def _exc(self) -> BannedTermsUnsetError:
        return BannedTermsUnsetError.for_key("banned_terms", "T3_BANNED_TERMS")

    def test_resolve_raises_unreadable_not_plain_unset(self, tmp_path: Path) -> None:
        with pytest.raises(BannedTermsUnreadableError):
            resolve_banned_terms(db_path=_corrupt_db(tmp_path))

    def test_report_unset_fails_closed_without_banned_terms_required(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # banned_terms_required is FALSE here (no row) — an unreadable store still fails closed.
        exc = BannedTermsUnreadableError.for_store("banned_terms", "T3_BANNED_TERMS")
        assert report_unset(exc, db_path=_corrupt_db(tmp_path)) == 2
        assert "could not be READ" in capsys.readouterr().err

    def test_verdict_is_unreadable_not_allow(self, tmp_path: Path) -> None:
        unreadable = BannedTermsUnreadableError.for_store("banned_terms", "T3_BANNED_TERMS")
        assert resolve_unset_verdict(unreadable, db_path=_corrupt_db(tmp_path)) is UnsetVerdict.UNREADABLE
        assert resolve_unset_verdict(self._exc(), db_path=_empty_db(tmp_path)) is UnsetVerdict.ALLOW

    def test_main_fails_closed_exit_2(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_BANNED_TERMS", raising=False)
        monkeypatch.setenv("T3_CONFIG_DB", str(_corrupt_db(tmp_path)))
        assert main([]) == 2

    def test_locked_store_fails_closed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # The reported cause verbatim: a busy writer holding the DB while the hook reads.
        monkeypatch.delenv("T3_BANNED_TERMS", raising=False)
        db = _seed(tmp_path, ["acme"])
        monkeypatch.setenv("T3_CONFIG_DB", str(db))
        writer = sqlite3.connect(str(db))
        writer.isolation_level = None
        writer.execute("BEGIN EXCLUSIVE")
        try:
            assert main([]) == 2
        finally:
            writer.rollback()
            writer.close()

    def test_absent_store_still_warns_and_allows(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The preserved #3247 case: nothing to read is CONFIRMED absence, not a read failure.
        monkeypatch.delenv("T3_BANNED_TERMS", raising=False)
        monkeypatch.setenv("T3_CONFIG_DB", str(tmp_path / "absent.sqlite3"))
        assert main([]) == 0
        assert "WARNING" in capsys.readouterr().err


class TestOwnRepoUrlCarveOut:
    """A term appearing ONLY inside a recognized private work-item/repo URL is allow-listed (#3251).

    The carve-out is scoped to the pre-commit ``--diff-only`` path
    (:func:`_diff_only_report`); its full-file fallback runs when the staged diff
    is unresolvable (a non-git ``tmp_path``). The posting gate's full-file scan
    keeps its own downstream ``deny.py`` own-repo-URL warn, so this pre-commit
    carve-out never suppresses it.
    """

    def _scan(self, tmp_path: Path, content: str, terms: tuple[str, ...], db: Path) -> list[str]:
        scanned = tmp_path / "notes.md"
        scanned.write_text(content, encoding="utf-8")
        return _diff_only_report([str(scanned)], terms, tmp_path, config_path=db)

    def _private_repos_db(self, tmp_path: Path) -> Path:
        return _seed_setting(tmp_path, "private_repos", value=["gitlab.example.com/acme-eng"])

    def test_term_only_in_own_repo_url_is_allowlisted(self, tmp_path: Path) -> None:
        db = self._private_repos_db(tmp_path)
        report = self._scan(
            tmp_path, "See https://gitlab.example.com/acme-eng/tracker/-/issues/5 for the fix.\n", ("acme-eng",), db
        )
        assert report == []

    def test_bare_term_outside_url_still_flags(self, tmp_path: Path) -> None:
        db = self._private_repos_db(tmp_path)
        report = self._scan(
            tmp_path, "acme-eng leaked. https://gitlab.example.com/acme-eng/tracker/-/issues/5\n", ("acme-eng",), db
        )
        assert report
        assert any("BANNED TERM" in line for line in report)

    def test_term_in_foreign_url_still_flags(self, tmp_path: Path) -> None:
        db = self._private_repos_db(tmp_path)
        report = self._scan(
            tmp_path, "https://gitlab.example.com/other-org/tracker/-/issues/5 mentions acme-eng.\n", ("acme-eng",), db
        )
        assert report != []

    def test_no_private_repos_still_flags(self, tmp_path: Path) -> None:
        # Without a ``private_repos`` allowlist there is no own-repo URL to carve
        # out, so the term inside the URL still flags (fail-safe-to-block).
        report = self._scan(
            tmp_path, "https://gitlab.example.com/acme-eng/tracker/-/issues/5\n", ("acme-eng",), _empty_db(tmp_path)
        )
        assert report != []

    def test_full_file_scan_does_not_carve_out_the_posting_gate_surface(self, tmp_path: Path) -> None:
        # The posting-gate full-file scan must STILL flag the term so the gate's
        # own downstream deny.py own-repo-URL warn fires (not silently suppressed).
        scanned = tmp_path / "notes.md"
        scanned.write_text("https://gitlab.example.com/acme-eng/tracker/-/issues/5\n", encoding="utf-8")
        assert _full_file_report([str(scanned)], ("acme-eng",)) != []
