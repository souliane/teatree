"""Captured unshipped work is surfaced with an age (#3891).

Functional: rows are written through the REAL capture writer
(:func:`teatree.core.cleanup.unshipped_work._record_capture`), the real check runs,
and the real stdout is asserted. That writer matters here — the capture pass re-runs
on every non-dry-run sweep over every kept checkout, so a test that manufactured its
rows with a raw queryset would never exercise a re-capture and would prove the
formatting while the age it formats stayed permanently zero.

Only the passage of time is manufactured, since a test cannot wait a month for it.
"""

import io
from collections.abc import Callable, Sequence
from contextlib import redirect_stdout
from datetime import timedelta
from pathlib import Path
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from teatree.cli.doctor.checks_unshipped_work import check_unshipped_work
from teatree.core.cleanup.unshipped_work import UnshippedWork, _record_capture
from teatree.core.models import UnshippedWorkRecord


def _echoes(check: Callable[[], bool]) -> tuple[bool, str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        ok = check()
    return ok, buf.getvalue()


def _capture(
    path: str,
    *,
    branch: str = "",
    dirty: Sequence[str] = (),
    unpushed: Sequence[str] = (),
    unreadable: str = "",
) -> UnshippedWorkRecord:
    """Write (or re-write) one checkout's record exactly as a reaping pass does."""
    return _record_capture(
        Path(path),
        branch,
        "",
        Path(f"/artifacts/unshipped-work/{Path(path).name}"),
        UnshippedWork(dirty_paths=list(dirty), unpushed_commits=list(unpushed), unreadable=unreadable),
    )


def _backdate(record: UnshippedWorkRecord, *, days: int) -> None:
    """Age BOTH stamps — the one thing only real time could supply.

    A queryset UPDATE rather than a ``save()``: ``first_captured_at`` is
    ``auto_now_add`` and ``captured_at`` is ``auto_now``, so neither honours an
    assigned value. Both move together, so the fixture is a genuinely old row and
    not one that merely looks old on the stamp under test.
    """
    when = timezone.now() - timedelta(days=days, hours=1)
    UnshippedWorkRecord.objects.filter(pk=record.pk).update(first_captured_at=when, captured_at=when)


class UnshippedWorkCheckTest(TestCase):
    def test_an_empty_ledger_says_nothing(self) -> None:
        ok, out = _echoes(check_unshipped_work)

        assert ok is True
        assert out == ""

    def test_a_captured_checkout_is_named_with_what_it_holds_and_how_long(self) -> None:
        record = _capture(
            "/w/scratch/agent-a1",
            branch="fix/lost-work",
            dirty=["src/a.py", "src/b.py"],
            unpushed=["abc1234 feat: never pushed"],
        )
        _backdate(record, days=9)

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

    def test_a_re_capture_does_not_reset_the_reported_age(self) -> None:
        # The regression. Capture re-runs on every non-dry-run sweep for every kept
        # checkout, so an age read off the LAST capture is reset continuously and
        # reads zero forever on the host that sweeps most — a surface that always
        # says "just now" has stopped reporting anything at all.
        record = _capture("/w/scratch/long-held", branch="fix/old", dirty=["src/a.py"])
        _backdate(record, days=30)

        _capture("/w/scratch/long-held", branch="fix/old", dirty=["src/a.py", "src/b.py"])

        _ok, out = _echoes(check_unshipped_work)

        assert "oldest 30d old" in out, "a re-capture must not reset how long the work has been waiting"
        assert "2 dirty path(s)" in out, "the re-capture still refreshes WHAT the checkout holds"
        assert UnshippedWorkRecord.objects.count() == 1, "one checkout keeps one row"

    def test_the_oldest_record_sets_the_headline_age(self) -> None:
        _backdate(_capture("/w/scratch/new", dirty=["src/a.py"]), days=0)
        _backdate(_capture("/w/scratch/old", dirty=["src/a.py"]), days=31)

        _ok, out = _echoes(check_unshipped_work)

        assert "2 checkout(s)" in out
        assert "oldest 31d old" in out

    def test_a_checkout_whose_state_could_not_be_read_still_counts_as_holding_work(self) -> None:
        _capture("/w/scratch/unreadable", unreadable="fatal: bad object")

        _ok, out = _echoes(check_unshipped_work)

        assert "unreadable here" in out
        assert "fatal: bad object" in out

    def test_an_unreadable_record_does_not_blame_git_for_a_venue_miss(self) -> None:
        _capture("/w/scratch/elsewhere", unreadable="/w/scratch/elsewhere records its git dir at /other/venue")

        _ok, out = _echoes(check_unshipped_work)

        assert "records its git dir at /other/venue" in out
        assert "git could not read" not in out

    def test_a_record_with_no_branch_is_still_reported(self) -> None:
        _capture("/w/scratch/detached", dirty=["src/a.py"])

        _ok, out = _echoes(check_unshipped_work)

        assert "(no branch)" in out

    def test_a_long_ledger_is_capped_and_says_how_many_it_hid(self) -> None:
        for index in range(13):
            _backdate(_capture(f"/w/scratch/agent-{index:02d}", dirty=["src/a.py"]), days=index)

        _ok, out = _echoes(check_unshipped_work)

        assert "13 checkout(s)" in out
        assert "… and 3 more" in out

    def test_an_hours_old_record_reads_in_hours(self) -> None:
        _backdate(_capture("/w/scratch/fresh", dirty=["src/a.py"]), days=0)

        _ok, out = _echoes(check_unshipped_work)

        assert "1h old" in out

    def test_a_freshly_written_record_ages_from_the_writer_own_stamp(self) -> None:
        # No backdating anywhere: the real writer supplies the timestamp, which is
        # what makes the re-capture test above a comparison rather than a tautology.
        _capture("/w/scratch/brand-new", dirty=["src/a.py"])

        _ok, out = _echoes(check_unshipped_work)

        assert "oldest 0m old" in out

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
