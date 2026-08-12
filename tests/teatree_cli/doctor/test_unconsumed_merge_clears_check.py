"""``_check_unconsumed_merge_clears`` — the standing merge-backlog finding (#4250).

The state nothing reported: 87 ``MergeClear`` rows authorising a merge that was never
executed, the oldest 19 days old, while the S4 age signal returned ``None`` (it joined a
field the rows do not carry), the sweep logged an unrelated reason at INTERNAL audience,
and ``MergeAudit`` was correctly empty because nothing had merged. Four surfaces, and the
union of them was silence. This check is the surface that reports it.

Its first cut then read "no local ``MergeAudit``" as "the merge stalled" and was 6/6
false on live data — every one of those PRs had merged outside the keystone. So a FAIL
now requires positive evidence the PR is OPEN, and the forge reader is INJECTED: every
case below seeds an explicit fake, because the production default is UNVERIFIED and a
case that seeded no reader would pass green while asserting nothing.

Seeded with ``ticket=None`` throughout — that is the production norm, and the reason the
backlog was invisible.
"""

from datetime import timedelta
from unittest.mock import patch

import django.test
from django.utils import timezone

import teatree.cli.doctor.run_checks as doctor_runner
from teatree.cli.doctor.checks_loop import _check_unconsumed_merge_clears
from teatree.cli.doctor.run_checks import _run_loop_intent_gates
from teatree.core.backend_protocols import PrOpenState
from teatree.core.factory.clear_liveness_report import LISTED
from teatree.core.factory.merge_backlog import STALE_CLEAR_HOURS
from teatree.core.merge.clear_liveness import PROBE_CAP
from tests.factories import MergeAuditFactory, MergeClearFactory

_QUERY = "teatree.core.factory.clear_liveness_report.unconsumed_actionable_clear_rows"
_READER = "teatree.backends.loader.pr_open_state"


def _reads(state: str) -> object:
    def read(pr_url: str) -> str:
        return state

    return read


class UnconsumedClearCheckBase(django.test.TestCase):
    SLUG = "souliane/teatree"

    def setUp(self) -> None:
        self.now = timezone.now()

    def _stranded(self, *, pr_id: int, hours: float, slug: str = SLUG) -> None:
        MergeClearFactory(ticket=None, pr_id=pr_id, slug=slug, issued_at=self.now - timedelta(hours=hours))

    def _run(self, *, forge: str = PrOpenState.OPEN) -> bool:
        with patch(_READER, _reads(forge)):
            return _check_unconsumed_merge_clears()

    def _message(self, *, forge: str = PrOpenState.OPEN) -> str:
        with patch("typer.echo") as echo:
            self.verdict = self._run(forge=forge)
        return "\n".join(str(call.args[0]) for call in echo.call_args_list)


class TestUnconsumedMergeClearsCheck(UnconsumedClearCheckBase):
    def test_an_empty_backlog_passes(self) -> None:
        assert self._run() is True

    def test_a_fresh_authorisation_is_not_yet_a_finding(self) -> None:
        self._stranded(pr_id=4250, hours=STALE_CLEAR_HOURS - 1)
        assert self._run() is True

    def test_an_aged_ticketless_authorisation_whose_pr_is_open_fails(self) -> None:
        self._stranded(pr_id=4250, hours=STALE_CLEAR_HOURS + 1)
        assert self._run(forge=PrOpenState.OPEN) is False

    def test_a_merged_pr_is_never_a_fail(self) -> None:
        # Mirrors live CLEAR 557 / souliane/teatree#4142: unconsumed, no MergeAudit,
        # 187h old — and MERGED. This is a row the check paged the owner about daily.
        self._stranded(pr_id=4142, hours=187)
        assert self._run(forge=PrOpenState.MERGED) is True

    def test_a_closed_pr_is_never_a_fail(self) -> None:
        self._stranded(pr_id=4143, hours=187)
        assert self._run(forge=PrOpenState.CLOSED) is True

    def test_an_unresolvable_pr_is_no_finding_at_all(self) -> None:
        # Mirrors rows 618/619 (#4242/#4343), which `gh` resolves to nothing. No
        # evidence is not a finding — and emphatically not a FAIL.
        self._stranded(pr_id=4242, hours=187)
        message = self._message(forge=PrOpenState.UNKNOWN)

        assert self.verdict is True
        assert message == ""

    def test_a_consumed_authorisation_is_not_a_finding(self) -> None:
        MergeClearFactory(
            ticket=None,
            pr_id=4252,
            issued_at=self.now - timedelta(hours=STALE_CLEAR_HOURS + 10),
            consumed_at=self.now,
        )
        assert self._run() is True

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
        assert self._run() is True

    def test_a_clear_whose_repo_no_overlay_declares_is_still_reported(self) -> None:
        # The report is deliberately GLOBAL: scoping it per overlay is exactly how a
        # foreign-repo authorisation would go unreported again.
        self._stranded(pr_id=4254, hours=STALE_CLEAR_HOURS + 5, slug="someone-else/other-repo")
        assert self._run(forge=PrOpenState.OPEN) is False

    def test_a_crashed_read_degrades_to_ok(self) -> None:
        with patch(_QUERY, side_effect=RuntimeError("db gone")):
            assert self._run() is True


class TestFindingMessage(UnconsumedClearCheckBase):
    def test_it_names_the_ref_the_age_and_the_remedy(self) -> None:
        self._stranded(pr_id=4250, hours=STALE_CLEAR_HOURS + 200)
        message = self._message(forge=PrOpenState.OPEN)

        assert self.verdict is False
        assert "souliane/teatree#4250" in message
        assert "unconsumed past 48h" in message
        assert "re-issue the CLEAR at the live head" in message

    def test_it_leads_with_the_oldest_not_an_arbitrary_row(self) -> None:
        self._stranded(pr_id=4260, hours=STALE_CLEAR_HOURS + 1)
        self._stranded(pr_id=4261, hours=STALE_CLEAR_HOURS + 300)
        headline = self._message(forge=PrOpenState.OPEN).splitlines()[0]

        assert "#4261" in headline
        assert "#4260" not in headline

    def test_a_long_backlog_summarises_its_tail_instead_of_dumping_it(self) -> None:
        for offset in range(LISTED + 3):
            self._stranded(pr_id=5000 + offset, hours=STALE_CLEAR_HOURS + 10 + offset)
        message = self._message(forge=PrOpenState.OPEN)

        assert "…and 3 more standing authorisation(s)." in message

    def test_the_existing_backlog_is_reported_not_adopted_as_a_new_baseline(self) -> None:
        # Acceptance clause: a fix that starts the clock now and leaves the standing rows
        # invisible has not fixed it. Every aged row is counted, however old.
        for offset in range(4):
            self._stranded(pr_id=6000 + offset, hours=24 * 19 + offset)
        message = self._message(forge=PrOpenState.OPEN)

        assert "4 merge authorisation(s) unconsumed" in message

    def test_a_settled_authorisation_warns_with_the_reconcile_remedy(self) -> None:
        self._stranded(pr_id=4142, hours=187)
        message = self._message(forge=PrOpenState.MERGED)

        assert self.verdict is True
        assert message.startswith("WARN  1 merge authorisation(s) unconsumed past 48h whose PR already merged")
        assert "reconcile-clears" in message
        assert "FAIL" not in message

    def test_the_probe_cap_is_reported_not_silent(self) -> None:
        for offset in range(PROBE_CAP + 2):
            self._stranded(pr_id=7000 + offset, hours=STALE_CLEAR_HOURS + 10 + offset)
        message = self._message(forge=PrOpenState.MERGED)

        assert "WARN  2 further aged authorisation(s) were not checked against the" in message
        assert "unknown, not healthy" in message


class TestDoctorWiring(UnconsumedClearCheckBase):
    def test_the_fail_reaches_the_doctor_run_verdict(self) -> None:
        # Wired into the aggregation, not merely defined: a check no orchestration list
        # evaluates is dead authority, and its FAIL would never reach an operator. The
        # sibling gates are pinned GREEN so the aggregate can only flip on this one.
        with (
            patch(_READER, _reads(PrOpenState.OPEN)),
            patch.object(doctor_runner, "_check_intent_freshness", return_value=True),
            patch.object(doctor_runner, "_check_intake_budget_deadlock", return_value=True),
            patch.object(doctor_runner, "_check_loop_schedule_liveness", return_value=True),
            patch.object(doctor_runner, "_check_t3_master_unheld_while_loops_tick", return_value=True),
        ):
            assert _run_loop_intent_gates() is True
            self._stranded(pr_id=4250, hours=STALE_CLEAR_HOURS + 1)
            assert _run_loop_intent_gates() is False
