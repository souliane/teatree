"""``_check_aged_sweep_skips`` — one standing finding, never one line per row (#4523).

The live ledger held 41 rows and the check printed 41 WARN lines, so a single unchanging
condition read as 41 incidents. Volume is not incident count: the surface reports the count
once, names the oldest few stalls, and never faults on a deliberate park.
"""

import contextlib
import datetime as dt
import io

import django.test
from django.utils import timezone

from teatree.cli.doctor.checks_loop import _check_aged_sweep_skips
from teatree.core.models import SkipObservation, SweepSkipStreak
from teatree.loop.pr_sweep_skip_surface import SURFACE_AFTER_TICKS


def _stand(*, pr_id: int, reason: str, slug: str = "o/r", first_seen: dt.datetime | None = None) -> None:
    moment = first_seen or timezone.now()
    for tick in range(SURFACE_AFTER_TICKS):
        SweepSkipStreak.objects.observe(
            SkipObservation(slug=slug, pr_id=pr_id, reason=reason, url=""),
            now=moment + dt.timedelta(seconds=tick),
        )


def _run() -> tuple[bool, list[str]]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        ok = _check_aged_sweep_skips()
    return ok, [line for line in out.getvalue().splitlines() if line]


class TestAgedSweepSkipsCheck(django.test.TestCase):
    def test_an_empty_ledger_says_nothing(self) -> None:
        assert _run() == (True, [])

    def test_forty_one_standing_prs_are_one_finding_not_forty_one_lines(self) -> None:
        oldest = timezone.now() - dt.timedelta(hours=362, minutes=5)
        for pr_id in range(1, 42):
            _stand(pr_id=pr_id, reason="ci_pending", first_seen=oldest + dt.timedelta(seconds=pr_id))

        ok, lines = _run()

        assert ok is False
        assert lines[0] == (
            "WARN  41 PR(s) standing at 3+ consecutive merge-sweep skips (362h oldest) "
            "— 41 stall(s), 0 deliberate park(s)."
        )
        assert len(lines) == 5
        assert lines[-1] == "WARN    +38 more stall(s) not shown."

    def test_the_detail_lines_carry_the_pr_link(self) -> None:
        _stand(pr_id=4055, reason="ci_pending")

        _, lines = _run()

        assert lines[1].endswith("https://github.com/o/r/pull/4055")
        assert "no URL recorded" not in "\n".join(lines)

    def test_a_deliberate_park_is_counted_but_never_a_fault(self) -> None:
        _stand(pr_id=4414, reason="draft")

        ok, lines = _run()

        expected = (
            "OK    1 PR(s) standing at 3+ consecutive merge-sweep skips (0h oldest) — 0 stall(s), 1 deliberate park(s)."
        )

        assert ok is True
        assert lines == [expected]

    def test_a_stall_beside_parks_still_faults(self) -> None:
        _stand(pr_id=4414, reason="draft")
        _stand(pr_id=4055, reason="ci_red")

        ok, lines = _run()

        assert ok is False
        assert lines[0].endswith("— 1 stall(s), 1 deliberate park(s).")
        assert [line for line in lines if "#4414" in line] == []
