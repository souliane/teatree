"""The per-pass promotion cap that bounds how much backlog ONE dream pass drains (#4176).

Every promoting phase — core-gap memory promotion, automatable asks, compliance
escalation — funnels through ``umbrella_ledger.promote_gap``, which was unbounded. The
first pass with the toggles on would therefore schedule one coding task per backlog row
in a single night (measured: 62 CORE_GAP rows waiting). The budget bounds that, spends
only on NEW work so idempotent re-visits cannot starve fresh gaps, and reports what it
turned away rather than truncating silently.
"""

from unittest.mock import MagicMock, patch

from django.test import TestCase

from teatree.core.backend_protocols import CodeHostBackend
from teatree.core.models import ConsolidatedMemory
from teatree.core.models.ticket import Ticket
from teatree.loops.dream import promote_memory
from teatree.loops.dream import umbrella_ledger as ul
from teatree.loops.dream.pass_config import PromotionBudget, promotion_cap

UMBRELLA = "https://github.com/souliane/teatree/issues/2663"


def _fake_host(*, body: str = "## Open gaps\n") -> CodeHostBackend:
    """A STATEFUL umbrella: its body persists across writes, as the real issue does.

    Statefulness is load-bearing here — a host that re-reads a pristine body makes every
    already-promoted gap look new, so the "a repeat costs no budget" property cannot be
    observed at all.
    """
    state = {"body": body}

    def _update(**kwargs: object) -> dict[str, int]:
        state["body"] = str(kwargs["body"])
        return {"number": 2663}

    host = MagicMock(spec=CodeHostBackend)
    host.get_issue.side_effect = lambda *_a, **_k: {"body": state["body"]}
    host.update_issue.side_effect = _update
    return host


def _gap(key: str) -> ul.GapSpec:
    return ul.GapSpec(gap_key=key, title=f"Fix the gate {key}", cluster_key=key)


def _memory(key: str) -> ConsolidatedMemory:
    return ConsolidatedMemory.objects.create(
        cluster_key=key,
        rule=f"Run the tree-wide health gate before any push ({key}).",
        source_files=[f"feedback_{key}.md"],
        durable_destination="skills/ship/SKILL.md",
        member_count=1,
        max_member_weight=90,
        verified_citation="pushed without running the gate, CI went red",
    )


class PromotionCapResolutionTestCase(TestCase):
    """The cap number is data: env wins, then the DB key, then a bounded default."""

    def test_default_is_bounded_not_unlimited(self) -> None:
        with (
            patch.dict("os.environ", {"T3_DREAM_PROMOTION_CAP": ""}, clear=False),
            patch("teatree.loops.dream.pass_config.dream_table", return_value={}),
        ):
            assert promotion_cap() == 5

    def test_env_overrides_the_db_key(self) -> None:
        with (
            patch.dict("os.environ", {"T3_DREAM_PROMOTION_CAP": "2"}, clear=False),
            patch("teatree.loops.dream.pass_config.dream_table", return_value={"promotion_cap": 9}),
        ):
            assert promotion_cap() == 2

    def test_db_key_is_read_when_no_env(self) -> None:
        with (
            patch.dict("os.environ", {"T3_DREAM_PROMOTION_CAP": ""}, clear=False),
            patch("teatree.loops.dream.pass_config.dream_table", return_value={"promotion_cap": 3}),
        ):
            assert promotion_cap() == 3

    def test_garbage_degrades_to_the_default_rather_than_raising(self) -> None:
        with (
            patch.dict("os.environ", {"T3_DREAM_PROMOTION_CAP": "not-a-number"}, clear=False),
            patch("teatree.loops.dream.pass_config.dream_table", return_value={}),
        ):
            assert promotion_cap() == 5

    def test_zero_means_unbounded(self) -> None:
        with (
            patch.dict("os.environ", {"T3_DREAM_PROMOTION_CAP": "0"}, clear=False),
            patch("teatree.loops.dream.pass_config.dream_table", return_value={}),
        ):
            assert PromotionBudget.from_config().exhausted is False

    def test_unbounded_budget_never_exhausts_however_much_it_spends(self) -> None:
        budget = PromotionBudget(remaining=None)
        for _ in range(50):
            budget.spend()
        assert budget.exhausted is False
        assert budget.deferred == 0


class PromoteGapRespectsTheBudgetTestCase(TestCase):
    """``promote_gap`` is the ONE place a gap becomes a scheduled fix — and the ONE spender."""

    def test_a_gap_within_budget_is_promoted_and_costs_one(self) -> None:
        budget = PromotionBudget(remaining=2)
        outcome = ul.promote_gap(_fake_host(), umbrella_url=UMBRELLA, gap=_gap("gap-1"), budget=budget)
        assert outcome.scheduled is True
        assert outcome.deferred is False
        assert budget.remaining == 1

    def test_an_exhausted_budget_defers_without_writing_or_scheduling(self) -> None:
        host = _fake_host()
        budget = PromotionBudget(remaining=0)
        outcome = ul.promote_gap(host, umbrella_url=UMBRELLA, gap=_gap("gap-1"), budget=budget)
        assert outcome.deferred is True
        assert outcome.scheduled is False
        assert outcome.checkbox_added is False
        host.update_issue.assert_not_called()
        assert not Ticket.objects.filter(extra__dream_gap_key="gap-1").exists()
        assert budget.deferred == 1

    def test_an_already_promoted_gap_costs_nothing_and_cannot_starve_new_ones(self) -> None:
        # A re-visited gap does no new work, so charging it would let a steady-state
        # backlog burn the whole cap on repeats and never reach a fresh gap.
        host = _fake_host(body="## Open gaps\n- [ ] Fix the gate gap-1 <!-- dream-gap gap-1 -->\n")
        ul.schedule_gap_fix(umbrella_url=UMBRELLA, gap_key="gap-1", title="Fix the gate gap-1", cluster_key="gap-1")
        budget = PromotionBudget(remaining=1)
        repeat = ul.promote_gap(host, umbrella_url=UMBRELLA, gap=_gap("gap-1"), budget=budget)
        assert repeat.scheduled is False
        assert repeat.checkbox_added is False
        assert budget.remaining == 1
        fresh = ul.promote_gap(host, umbrella_url=UMBRELLA, gap=_gap("gap-2"), budget=budget)
        assert fresh.scheduled is True
        assert budget.remaining == 0

    def test_no_budget_stays_unbounded_so_existing_callers_are_unchanged(self) -> None:
        for i in range(4):
            outcome = ul.promote_gap(_fake_host(), umbrella_url=UMBRELLA, gap=_gap(f"gap-{i}"))
            assert outcome.scheduled is True

    def test_a_withheld_gap_costs_nothing(self) -> None:
        budget = PromotionBudget(remaining=1)
        with patch("teatree.loops.dream.umbrella_ledger.banned_terms_scanner.scan_text", return_value="a-banned-term"):
            outcome = ul.promote_gap(_fake_host(), umbrella_url=UMBRELLA, gap=_gap("gap-1"), budget=budget)
        assert outcome.withheld is True
        assert budget.remaining == 1

    def test_a_dry_run_costs_nothing(self) -> None:
        budget = PromotionBudget(remaining=1)
        ul.promote_gap(_fake_host(), umbrella_url=UMBRELLA, gap=_gap("gap-1"), budget=budget, dry_run=True)
        assert budget.remaining == 1

    def test_one_budget_is_shared_across_phases_in_a_pass(self) -> None:
        # "Per-pass", not "per-phase": two phases handed the SAME budget must not each
        # get the full cap, or one pass promotes cap x phases.
        budget = PromotionBudget(remaining=1)
        first = ul.promote_gap(_fake_host(), umbrella_url=UMBRELLA, gap=_gap("from-memory-phase"), budget=budget)
        second = ul.promote_gap(_fake_host(), umbrella_url=UMBRELLA, gap=_gap("from-asks-phase"), budget=budget)
        assert first.scheduled is True
        assert second.deferred is True


class DeferralIsNeverSilentTestCase(TestCase):
    """A cap that truncates without saying so reads as 'the backlog is drained'."""

    def test_the_summary_names_what_the_cap_turned_away(self) -> None:
        budget = PromotionBudget(remaining=0)
        ul.promote_gap(_fake_host(), umbrella_url=UMBRELLA, gap=_gap("gap-1"), budget=budget)
        assert "deferred 1 promotion(s)" in budget.summary
        assert "next pass" in budget.summary

    def test_a_pass_that_deferred_nothing_adds_no_clause(self) -> None:
        budget = PromotionBudget(remaining=1)
        ul.promote_gap(_fake_host(), umbrella_url=UMBRELLA, gap=_gap("gap-1"), budget=budget)
        assert budget.summary == ""


class CoreGapPromotionIsCappedTestCase(TestCase):
    """The measured risk: a 62-row CORE_GAP backlog drained in one unbounded pass."""

    def test_only_the_capped_number_of_core_gaps_is_promoted(self) -> None:
        for i in range(5):
            _memory(f"gap-{i}")
        outcomes = promote_memory.file_core_gap_tickets(
            _fake_host(), umbrella_url=UMBRELLA, budget=PromotionBudget(remaining=2)
        )
        assert sum(1 for o in outcomes if o.filed) == 2
        assert Ticket.objects.exclude(extra__dream_gap_key__isnull=True).count() == 2

    def test_deferred_core_gap_stays_in_the_needs_ticket_queue(self) -> None:
        # A deferred gap must be picked up by the NEXT pass, not stranded: the cap
        # spreads the backlog over nights, it does not drop any of it.
        host = _fake_host()
        for i in range(3):
            _memory(f"gap-{i}")
        promote_memory.file_core_gap_tickets(host, umbrella_url=UMBRELLA, budget=PromotionBudget(remaining=1))
        assert ConsolidatedMemory.objects.needs_ticket().count() == 3
        promote_memory.file_core_gap_tickets(host, umbrella_url=UMBRELLA, budget=PromotionBudget(remaining=1))
        assert Ticket.objects.exclude(extra__dream_gap_key__isnull=True).count() == 2

    def test_promoted_gap_is_visible_to_reconcile_merged_gaps(self) -> None:
        # The outcome-level ledger assertion: reconcile_merged_gaps selects on
        # extra.dream_gap_key, so a promoted gap must stamp it or the umbrella
        # checkboxes can never be ticked.
        _memory("gap-1")
        promote_memory.file_core_gap_tickets(_fake_host(), umbrella_url=UMBRELLA, budget=PromotionBudget(remaining=1))
        ticket = Ticket.objects.get(extra__dream_gap_key="gap-1")
        assert ticket.extra["dream_umbrella_url"] == UMBRELLA
        assert ul._in_flight_gap_tickets() == [ticket]
