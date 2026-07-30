"""Captured unshipped work is surfaced with an age (#3891).

Functional: real :class:`UnshippedWorkRecord` rows, the real check, and the real
stdout it writes. The capture half already wrote these rows; the gap was that no
surface read them, so work recorded before a reaper spared a checkout stayed
exactly as unobserved as before the capture existed.
"""

import io
from collections.abc import Callable
from contextlib import redirect_stdout
from datetime import timedelta
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from teatree.cli.doctor.checks_unshipped_work import check_unshipped_work
from teatree.core.models import UnshippedWorkRecord


def _echoes(check: Callable[[], bool]) -> tuple[bool, str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        ok = check()
    return ok, buf.getvalue()


def _record(path: str, *, days_old: int = 0, **fields: object) -> UnshippedWorkRecord:
    record = UnshippedWorkRecord.objects.create(checkout_path=path, **fields)
    # ``captured_at`` is auto_now, so age is set by an explicit UPDATE.
    UnshippedWorkRecord.objects.filter(pk=record.pk).update(
        captured_at=timezone.now() - timedelta(days=days_old, hours=1)
    )
    record.refresh_from_db()
    return record


class UnshippedWorkCheckTest(TestCase):
    def test_an_empty_ledger_says_nothing(self) -> None:
        ok, out = _echoes(check_unshipped_work)

        assert ok is True
        assert out == ""

    def test_a_captured_checkout_is_named_with_what_it_holds_and_how_long(self) -> None:
        _record(
            "/w/scratch/agent-a1",
            days_old=9,
            branch="fix/lost-work",
            dirty_paths=["src/a.py", "src/b.py"],
            unpushed_commits=["abc1234 feat: never pushed"],
            artifact_prefix="unshipped-work/agent-a1",
        )

        ok, out = _echoes(check_unshipped_work)

        assert ok is True, "owed work is a finding, not a broken invariant"
        assert "WARN" in out
        assert "1 checkout(s) hold captured unshipped work" in out
        assert "9d" in out
        assert "/w/scratch/agent-a1" in out
        assert "fix/lost-work" in out
        assert "2 dirty path(s)" in out
        assert "1 unpushed commit(s)" in out
        assert "unshipped-work/agent-a1" in out
        assert "workspace salvage" in out

    def test_the_oldest_record_sets_the_headline_age(self) -> None:
        _record("/w/scratch/new", days_old=0)
        _record("/w/scratch/old", days_old=31)

        _ok, out = _echoes(check_unshipped_work)

        assert "2 checkout(s)" in out
        assert "oldest 31d old" in out

    def test_a_checkout_whose_state_could_not_be_read_still_counts_as_holding_work(self) -> None:
        _record("/w/scratch/unreadable", unreadable="fatal: bad object")

        _ok, out = _echoes(check_unshipped_work)

        assert "state git could not read" in out
        assert "fatal: bad object" in out

    def test_a_record_with_no_branch_is_still_reported(self) -> None:
        _record("/w/scratch/detached")

        _ok, out = _echoes(check_unshipped_work)

        assert "(no branch)" in out

    def test_a_long_ledger_is_capped_and_says_how_many_it_hid(self) -> None:
        for index in range(13):
            _record(f"/w/scratch/agent-{index:02d}", days_old=index)

        _ok, out = _echoes(check_unshipped_work)

        assert "13 checkout(s)" in out
        assert "… and 3 more" in out

    def test_an_hours_old_record_reads_in_hours(self) -> None:
        _record("/w/scratch/fresh", days_old=0)

        _ok, out = _echoes(check_unshipped_work)

        assert "1h old" in out

    def test_an_unreadable_ledger_degrades_to_unverified_rather_than_crashing(self) -> None:
        boom = mock.patch.object(
            UnshippedWorkRecord.objects.__class__,
            "order_by",
            side_effect=RuntimeError("db down"),
        )
        boom.start()
        self.addCleanup(boom.stop)

        ok, out = _echoes(check_unshipped_work)

        assert ok is True
        assert "UNVERIFIED" in out
        assert "db down" in out
