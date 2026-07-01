"""The Django-free UserPromptSubmit fast path (#22).

Three ``UserPromptSubmit`` handlers booted Django in-process on every prompt
(``django.setup()`` is idempotent, so the first boot cost the whole ~8s cold UPS
tax even when there was nothing to inject). ``ups_fastpath`` removes that tax: a
pure-stdlib heartbeat write and two Django-free ``sqlite3`` existence probes let
the handlers stay Django-free on the common empty-backlog turn. These tests run
the probes against a REAL sqlite DB (via ``T3_CONFIG_DB``) and the write against a
real temp dir — no mocks — so the fail-open and byte-format behaviour is exercised
against actual sqlite.
"""

import json
import sqlite3
import sys
from pathlib import Path

import pytest

import hooks.scripts.hook_router  # noqa: F401 — puts hooks/scripts on sys.path and imports the bare siblings
from teatree.core import availability

ups = sys.modules["ups_fastpath"]


def _config_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, questions: bool = True, chat: bool = True) -> Path:
    """Build the PRIMARY DB with the inject tables and point cold_reader at it."""
    db = tmp_path / "db.sqlite3"
    conn = sqlite3.connect(db)
    try:
        if questions:
            conn.execute(
                "CREATE TABLE teatree_deferred_question ("
                "id INTEGER PRIMARY KEY, answered_at TEXT, applied_at TEXT, dismissed_at TEXT)"
            )
        if chat:
            conn.execute("CREATE TABLE teatree_pending_chat_injection (id INTEGER PRIMARY KEY, consumed_at TEXT)")
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setenv("T3_CONFIG_DB", str(db))
    return db


class TestRecordPresence:
    def test_writes_heartbeat_readable_by_availability(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # The write lands at ``canonical_config_db().parent / availability_presence`` and
        # is byte-compatible with ``PresenceHeartbeat.record`` — the availability reader
        # parses it back into a fresh turn carrying the session id.
        monkeypatch.setenv("T3_CONFIG_DB", str(tmp_path / "db.sqlite3"))
        ups.record_presence("sess-1")
        target = tmp_path / "availability_presence"
        assert target.is_file()
        turn = availability.PresenceHeartbeat(locate=lambda: target).last_user_turn()
        assert turn is not None
        assert turn.session_id == "sess-1"

    def test_on_disk_shape_matches_record(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_CONFIG_DB", str(tmp_path / "db.sqlite3"))
        ups.record_presence("s2")
        raw = (tmp_path / "availability_presence").read_text(encoding="utf-8")
        assert raw.endswith("\n")
        doc = json.loads(raw)
        assert set(doc) == {"at", "session"}
        assert doc["session"] == "s2"
        assert raw == json.dumps({"at": doc["at"], "session": "s2"}, sort_keys=True) + "\n"

    def test_unresolvable_data_dir_is_silent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # No mocks needed: force the reader import to fail so the data dir is unresolvable.
        monkeypatch.setattr(ups, "teatree_src_on_path", _raising_context)
        ups.record_presence("s3")  # must not raise

    def test_write_failure_is_swallowed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_CONFIG_DB", str(tmp_path / "db.sqlite3"))

        def _boom(*_a: object, **_k: object) -> tuple[int, str]:
            raise OSError

        monkeypatch.setattr(ups.tempfile, "mkstemp", _boom)
        ups.record_presence("s4")  # must not raise


class TestHasPendingQuestionWork:
    def test_empty_table_skips(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _config_db(tmp_path, monkeypatch)
        assert ups.has_pending_question_work() is False

    def test_pending_unanswered_row_has_work(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        db = _config_db(tmp_path, monkeypatch)
        _insert_question(db, answered=None, applied=None, dismissed=None)
        assert ups.has_pending_question_work() is True

    def test_answered_not_applied_row_has_work(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        db = _config_db(tmp_path, monkeypatch)
        _insert_question(db, answered="2026-01-01T00:00:00Z", applied=None, dismissed=None)
        assert ups.has_pending_question_work() is True

    def test_fully_resolved_rows_skip(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # Applied (answered+applied) and dismissed rows need no handling → skip.
        db = _config_db(tmp_path, monkeypatch)
        _insert_question(db, answered="2026-01-01T00:00:00Z", applied="2026-01-01T00:01:00Z", dismissed=None)
        _insert_question(db, answered=None, applied=None, dismissed="2026-01-01T00:02:00Z")
        assert ups.has_pending_question_work() is False

    def test_missing_db_fails_open(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_CONFIG_DB", str(tmp_path / "absent.sqlite3"))
        assert ups.has_pending_question_work() is True

    def test_missing_table_fails_open(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _config_db(tmp_path, monkeypatch, questions=False)  # DB exists, but no deferred_question table
        assert ups.has_pending_question_work() is True


class TestHasPendingChatWork:
    def test_empty_table_skips(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _config_db(tmp_path, monkeypatch)
        assert ups.has_pending_chat_work() is False

    def test_unconsumed_row_has_work(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        db = _config_db(tmp_path, monkeypatch)
        _commit(db, "INSERT INTO teatree_pending_chat_injection(consumed_at) VALUES(NULL)")
        assert ups.has_pending_chat_work() is True

    def test_all_consumed_skips(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        db = _config_db(tmp_path, monkeypatch)
        _commit(db, "INSERT INTO teatree_pending_chat_injection(consumed_at) VALUES('2026-01-01T00:00:00Z')")
        assert ups.has_pending_chat_work() is False

    def test_missing_db_fails_open(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_CONFIG_DB", str(tmp_path / "absent.sqlite3"))
        assert ups.has_pending_chat_work() is True


class TestHandlersSkipDjangoBootWhenIdle:
    """The whole point (#22): an empty backlog must NOT boot Django on the UPS hot path."""

    def test_inject_questions_skips_boot_when_no_work(self, monkeypatch: pytest.MonkeyPatch) -> None:
        boots = _arm_boot_spy(monkeypatch, has_work=("has_pending_question_work", False), boot_returns=True)
        sys.modules["hook_router"].handle_inject_pending_questions({"session_id": "s1"})
        assert boots == []  # returned BEFORE django.setup()

    def test_inject_questions_boots_when_work_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # boot returns False so the handler stops right after — the boot itself is the proof.
        boots = _arm_boot_spy(monkeypatch, has_work=("has_pending_question_work", True), boot_returns=False)
        sys.modules["hook_router"].handle_inject_pending_questions({"session_id": "s1"})
        assert boots == ["boot"]

    def test_inject_chat_skips_boot_when_no_work(self, monkeypatch: pytest.MonkeyPatch) -> None:
        boots = _arm_boot_spy(monkeypatch, has_work=("has_pending_chat_work", False), boot_returns=True)
        sys.modules["hook_router"].handle_inject_pending_chat({"session_id": "s1"})
        assert boots == []

    def test_inject_chat_boots_when_work_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        boots = _arm_boot_spy(monkeypatch, has_work=("has_pending_chat_work", True), boot_returns=False)
        sys.modules["hook_router"].handle_inject_pending_chat({"session_id": "s1"})
        assert boots == ["boot"]


def _arm_boot_spy(monkeypatch: pytest.MonkeyPatch, *, has_work: tuple[str, bool], boot_returns: bool) -> list[str]:
    """Stub the router's pre-check + record whether ``bootstrap_teatree_django`` fires."""
    router = sys.modules["hook_router"]
    boots: list[str] = []
    name, verdict = has_work
    monkeypatch.setattr(router, name, lambda: verdict)

    def _boot() -> bool:
        boots.append("boot")
        return boot_returns

    monkeypatch.setattr(router, "bootstrap_teatree_django", _boot)
    return boots


def _insert_question(db: Path, *, answered: str | None, applied: str | None, dismissed: str | None) -> None:
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO teatree_deferred_question(answered_at, applied_at, dismissed_at) VALUES(?, ?, ?)",
            (answered, applied, dismissed),
        )
        conn.commit()
    finally:
        conn.close()


def _commit(db: Path, sql: str) -> None:
    conn = sqlite3.connect(db)
    try:
        conn.execute(sql)
        conn.commit()
    finally:
        conn.close()


def _raising_context() -> object:
    msg = "no reader"
    raise RuntimeError(msg)
