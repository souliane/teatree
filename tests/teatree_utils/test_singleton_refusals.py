"""Tests for ``teatree.utils.singleton_refusals`` — the refusal-streak ledger (#3976).

A service whose singleton acquire loses the race exits and is restarted; nothing in the
process survives to notice it is the Nth identical loss. The ledger is that memory, so
the restart cycle can escalate instead of churning forever.
"""

from pathlib import Path

import pytest

from teatree.utils.singleton_refusals import (
    ESCALATION_THRESHOLD,
    clear_refusals,
    default_refusal_path,
    read_streak,
    record_refusal,
)


class TestRecordRefusal:
    def test_first_refusal_starts_the_streak(self, tmp_path: Path) -> None:
        streak = record_refusal("worker", fingerprint="foreign", path=tmp_path / "w.json")
        assert streak.count == 1
        assert streak.escalated is False

    def test_identical_reason_increments(self, tmp_path: Path) -> None:
        path = tmp_path / "w.json"
        counts = [record_refusal("worker", fingerprint="foreign", path=path).count for _ in range(3)]
        assert counts == [1, 2, 3]

    def test_a_different_reason_restarts_the_count(self, tmp_path: Path) -> None:
        path = tmp_path / "w.json"
        record_refusal("worker", fingerprint="foreign", path=path)
        record_refusal("worker", fingerprint="foreign", path=path)
        assert record_refusal("worker", fingerprint="sibling", path=path).count == 1

    def test_escalates_at_the_threshold(self, tmp_path: Path) -> None:
        path = tmp_path / "w.json"
        streaks = [record_refusal("worker", fingerprint="foreign", path=path) for _ in range(ESCALATION_THRESHOLD)]
        assert [s.escalated for s in streaks] == [False] * (ESCALATION_THRESHOLD - 1) + [True]

    def test_stays_escalated_past_the_threshold(self, tmp_path: Path) -> None:
        path = tmp_path / "w.json"
        for _ in range(ESCALATION_THRESHOLD):
            record_refusal("worker", fingerprint="foreign", path=path)
        assert record_refusal("worker", fingerprint="foreign", path=path).escalated is True

    def test_a_garbled_ledger_starts_over_rather_than_crashing(self, tmp_path: Path) -> None:
        path = tmp_path / "w.json"
        path.write_text("{not json", encoding="utf-8")
        assert record_refusal("worker", fingerprint="foreign", path=path).count == 1


class TestReadStreak:
    def test_absent_ledger_reads_as_no_streak(self, tmp_path: Path) -> None:
        assert read_streak("worker", path=tmp_path / "absent.json") is None

    def test_reads_back_what_was_recorded(self, tmp_path: Path) -> None:
        path = tmp_path / "w.json"
        record_refusal("worker", fingerprint="foreign", path=path)
        recorded = record_refusal("worker", fingerprint="foreign", path=path)
        read_back = read_streak("worker", path=path)
        assert read_back is not None
        assert (read_back.count, read_back.fingerprint) == (recorded.count, "foreign")

    def test_a_garbled_ledger_reads_as_no_streak(self, tmp_path: Path) -> None:
        path = tmp_path / "w.json"
        path.write_text("{not json", encoding="utf-8")
        assert read_streak("worker", path=path) is None


class TestClearRefusals:
    def test_a_successful_acquire_ends_the_streak(self, tmp_path: Path) -> None:
        path = tmp_path / "w.json"
        record_refusal("worker", fingerprint="foreign", path=path)
        clear_refusals("worker", path=path)
        assert read_streak("worker", path=path) is None

    def test_clearing_an_absent_ledger_is_a_no_op(self, tmp_path: Path) -> None:
        clear_refusals("worker", path=tmp_path / "absent.json")

    def test_clearing_an_unwritable_ledger_never_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # Clearing runs on the SUCCESS path — a bookkeeping failure must never take
        # down the worker that just acquired the singleton.
        unwritable = OSError("read-only filesystem")

        def _boom(*_args: object, **_kwargs: object) -> None:
            raise unwritable

        path = tmp_path / "w.json"
        record_refusal("worker", fingerprint="foreign", path=path)
        monkeypatch.setattr("teatree.utils.singleton_refusals.Path.unlink", _boom)
        clear_refusals("worker", path=path)


class TestDefaultRefusalPath:
    def test_sits_beside_the_lock_file_it_describes(self) -> None:
        assert default_refusal_path("worker").name == "worker.refusals.json"
