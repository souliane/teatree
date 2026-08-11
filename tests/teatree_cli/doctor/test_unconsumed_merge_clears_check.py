"""``_check_unconsumed_merge_clears`` — the standing merge-backlog FAIL (#4250).

The state nothing reported: 87 ``MergeClear`` rows authorising a merge that was never
executed, the oldest 19 days old, while the S4 age signal returned ``None`` (it joined a
field the rows do not carry), the sweep logged an unrelated reason at INTERNAL audience,
and ``MergeAudit`` was correctly empty because nothing had merged. Four surfaces, and the
union of them was silence. This check is the surface that reports it.

Seeded with ``ticket=None`` throughout — that is the production norm, and the reason the
backlog was invisible.
"""

from datetime import timedelta
from unittest.mock import patch

import django.test
from django.utils import timezone

import teatree.cli.doctor.app as doctor_app
from teatree.cli.doctor.app import _run_loop_intent_gates
from teatree.cli.doctor.checks_loop import _CLEAR_BACKLOG_LISTED, _check_unconsumed_merge_clears
from teatree.core.factory.merge_backlog import STALE_CLEAR_HOURS
from tests.factories import MergeAuditFactory, MergeClearFactory

_QUERY = "teatree.core.factory.merge_backlog.unconsumed_actionable_clears"


class UnconsumedClearCheckBase(django.test.TestCase):
    SLUG = "souliane/teatree"

    def setUp(self) -> None:
        self.now = timezone.now()

    def _stranded(self, *, pr_id: int, hours: float, slug: str = SLUG) -> None:
        MergeClearFactory(ticket=None, pr_id=pr_id, slug=slug, issued_at=self.now - timedelta(hours=hours))

    def _message(self) -> str:
        with patch("typer.echo") as echo:
            self.verdict = _check_unconsumed_merge_clears()
        return "\n".join(str(call.args[0]) for call in echo.call_args_list)


class TestUnconsumedMergeClearsCheck(UnconsumedClearCheckBase):
    def test_an_empty_backlog_passes(self) -> None:
        assert _check_unconsumed_merge_clears() is True

    def test_a_fresh_authorisation_is_not_yet_a_finding(self) -> None:
        self._stranded(pr_id=4250, hours=STALE_CLEAR_HOURS - 1)
        assert _check_unconsumed_merge_clears() is True

    def test_an_aged_ticketless_authorisation_fails(self) -> None:
        self._stranded(pr_id=4250, hours=STALE_CLEAR_HOURS + 1)
        assert _check_unconsumed_merge_clears() is False

    def test_a_consumed_authorisation_is_not_a_finding(self) -> None:
        MergeClearFactory(
            ticket=None,
            pr_id=4252,
            issued_at=self.now - timedelta(hours=STALE_CLEAR_HOURS + 10),
            consumed_at=self.now,
        )
        assert _check_unconsumed_merge_clears() is True

    def test_a_superseded_authorisation_is_not_a_finding(self) -> None:
        # The PR merged under a sibling CLEAR, so the orphaned older row is not a stall —
        # the #15 supersede exclusion must survive this new consumer of the same query.
        self._stranded(pr_id=4253, hours=STALE_CLEAR_HOURS + 10)
        merged = MergeClearFactory(
            ticket=None,
            pr_id=4253,
            slug=self.SLUG,
            issued_at=self.now - timedelta(hours=2),
            consumed_at=self.now,
        )
        MergeAuditFactory(clear=merged, merged_at=self.now)
        assert _check_unconsumed_merge_clears() is True

    def test_a_clear_whose_repo_no_overlay_declares_is_still_reported(self) -> None:
        # The report is deliberately GLOBAL: scoping it per overlay is exactly how a
        # foreign-repo authorisation would go unreported again.
        self._stranded(pr_id=4254, hours=STALE_CLEAR_HOURS + 5, slug="someone-else/other-repo")
        assert _check_unconsumed_merge_clears() is False

    def test_a_crashed_read_degrades_to_ok(self) -> None:
        with patch(_QUERY, side_effect=RuntimeError("db gone")):
            assert _check_unconsumed_merge_clears() is True


class TestFindingMessage(UnconsumedClearCheckBase):
    def test_it_names_the_ref_the_age_and_the_remedy(self) -> None:
        self._stranded(pr_id=4250, hours=STALE_CLEAR_HOURS + 200)
        message = self._message()

        assert self.verdict is False
        assert "souliane/teatree#4250" in message
        assert "unconsumed past 48h" in message
        assert "re-issue the CLEAR at the live head" in message

    def test_it_leads_with_the_oldest_not_an_arbitrary_row(self) -> None:
        self._stranded(pr_id=4260, hours=STALE_CLEAR_HOURS + 1)
        self._stranded(pr_id=4261, hours=STALE_CLEAR_HOURS + 300)
        headline = self._message().splitlines()[0]

        assert "#4261" in headline
        assert "#4260" not in headline

    def test_a_long_backlog_summarises_its_tail_instead_of_dumping_it(self) -> None:
        for offset in range(_CLEAR_BACKLOG_LISTED + 3):
            self._stranded(pr_id=5000 + offset, hours=STALE_CLEAR_HOURS + 10 + offset)
        message = self._message()

        assert "…and 3 more standing authorisation(s)." in message

    def test_the_existing_backlog_is_reported_not_adopted_as_a_new_baseline(self) -> None:
        # Acceptance clause: a fix that starts the clock now and leaves the standing rows
        # invisible has not fixed it. Every aged row is counted, however old.
        for offset in range(4):
            self._stranded(pr_id=6000 + offset, hours=24 * 19 + offset)
        message = self._message()

        assert "4 merge authorisation(s) unconsumed" in message


class TestDoctorWiring(UnconsumedClearCheckBase):
    def test_the_fail_reaches_the_doctor_run_verdict(self) -> None:
        # Wired into the aggregation, not merely defined: a check no orchestration list
        # evaluates is dead authority, and its FAIL would never reach an operator. The
        # sibling gates are pinned GREEN so the aggregate can only flip on this one.
        with (
            patch.object(doctor_app, "_check_intent_freshness", return_value=True),
            patch.object(doctor_app, "_check_intake_budget_deadlock", return_value=True),
            patch.object(doctor_app, "_check_loop_schedule_liveness", return_value=True),
            patch.object(doctor_app, "_check_t3_master_unheld_while_loops_tick", return_value=True),
        ):
            assert _run_loop_intent_gates() is True
            self._stranded(pr_id=4250, hours=STALE_CLEAR_HOURS + 1)
            assert _run_loop_intent_gates() is False
