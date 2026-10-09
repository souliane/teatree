"""A dream pass mints no ticket: its collected gaps queue on the umbrella host for the sweep (#4776).

Every promoting phase collects its gaps via ``consider()`` and ``promote_batch()`` appends
them to the umbrella host ticket's ``dream_gap_pending`` ledger, where the backlog sweep
folds each into an existing host. Batch tickets minted before this change (``dream_gap_batch``
+ ``dream_umbrella_url``) keep draining through ``reconcile_batches``; these tests build them
directly with real ``Ticket`` / ``ConsolidatedMemory`` rows and a STATEFUL fake code host.
"""

import tempfile
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest
from django.test import TestCase

from teatree.core.backend_protocols import CodeHostBackend
from teatree.core.models import ConsolidatedMemory
from teatree.core.models.dream_gap_ledger import pending_entries, record_gap_disposition
from teatree.core.models.task import Task
from teatree.core.models.ticket import Ticket
from teatree.loop.scanners.backlog_sweep import BacklogSweepScanner
from teatree.loops.dream import batch_promote as bp
from teatree.loops.dream.gap_attach import attach_dream_gaps
from teatree.loops.dream.promote_memory import file_core_gap_tickets
from teatree.loops.dream.umbrella_ledger import GapSpec, _stamp_memory_promoted, gap_title, is_promotion_anchor
from tests.factories import record_test_plan
from tests.teatree_loops.dream._own_umbrella import claims_self, ours

if TYPE_CHECKING:
    from teatree.core.models.types import DreamGapEntry

UMBRELLA = "https://github.com/souliane/teatree/issues/2663"
REPO = "souliane/teatree"


def _umbrella() -> Ticket:
    return Ticket.objects.get_or_create(issue_url=UMBRELLA, defaults={"overlay": "t3-teatree"})[0]


def _batch_ticket(keys: "list[str] | tuple[str, ...]", *, suffix: str = "1") -> Ticket:
    """A batch ticket as the pre-sweep pass minted it: manifest + umbrella key, memory stamped on its url."""
    ticket = Ticket.objects.create(
        issue_url=f"{UMBRELLA}#dream-batch={suffix}",
        role=Ticket.Role.AUTHOR,
        extra={
            "dream_gap_batch": [{"gap_key": key, "cluster_key": key} for key in keys],
            "dream_umbrella_url": UMBRELLA,
        },
    )
    for key in keys:
        _stamp_memory_promoted(key, anchor_url=ticket.issue_url)
    return ticket


def _fake_host(*, body: str = "## Open gaps\n") -> CodeHostBackend:
    """A STATEFUL umbrella: its body persists across writes, as the real issue does."""
    state = {"body": body}

    def _update(**kwargs: object) -> dict[str, int]:
        state["body"] = str(kwargs["body"])
        return {"number": 2663}

    host = claims_self(MagicMock(spec=CodeHostBackend))
    host.get_issue.side_effect = lambda *_a, **_k: ours({"body": state["body"]})
    host.update_issue.side_effect = _update
    host.repo_for_issue_url.return_value = REPO
    return host


def _gap(key: str, *, title: str | None = None) -> GapSpec:
    return GapSpec(gap_key=key, title=title or f"Fix the gate {key}", cluster_key=key)


def _memory(
    *, key: str = "gap-1", binding: bool = False, source_files: list | None = None, **columns: str
) -> ConsolidatedMemory:
    return ConsolidatedMemory.objects.create(
        cluster_key=key,
        source_files=source_files if source_files is not None else [f"feedback_{key}.md"],
        is_binding=binding,
        member_count=1,
        max_member_weight=90,
        **{
            "rule": f"Run the tree-wide health gate before any push ({key}).",
            "durable_destination": "skills/ship/SKILL.md",
            "verified_citation": "pushed without running the gate, CI went red",
            **columns,
        },
    )


def _host_with_gap_a_and_b() -> CodeHostBackend:
    body = (
        "## Open gaps\n"
        + "\n".join(f"- [ ] Fix the gate {k} <!-- dream-gap {k} -->" for k in ["gap-a", "gap-b"])
        + "\n"
    )
    host = claims_self(MagicMock(spec=CodeHostBackend))
    state: dict[str, str] = {"body": body}

    def _update(**kwargs: object) -> dict[str, int]:
        state["body"] = str(kwargs["body"])
        return {"number": 2663}

    host.get_issue.side_effect = lambda *_a, **_k: ours({"body": state["body"], "state": "merged"})
    host.update_issue.side_effect = _update
    return host


def _retired(key: str) -> bool:
    row = ConsolidatedMemory.objects.get(cluster_key=key)
    return row.disposition == ConsolidatedMemory.Disposition.RESOLVED_RETIRED


class ConsiderTestCase(TestCase):
    """``consider()`` grounds/withholds/dedups a gap without writing or scheduling."""

    def test_a_new_gap_is_queued_and_nothing_is_written(self) -> None:
        batch = bp.PromotionBatch()
        outcome = batch.consider(gap=_gap("gap-1"))
        assert outcome.queued is True
        assert batch.pending == [_gap("gap-1")]

    def test_a_banned_term_title_is_withheld_and_not_queued(self) -> None:
        batch = bp.PromotionBatch()
        with patch("teatree.loops.dream.umbrella_ledger.banned_terms_scanner.scan_text", return_value="a-banned-term"):
            outcome = batch.consider(gap=_gap("gap-1"))
        assert outcome.withheld is True
        assert outcome.queued is False
        assert batch.pending == []
        assert batch.withheld == 1

    def test_dry_run_queues_nothing(self) -> None:
        batch = bp.PromotionBatch()
        outcome = batch.consider(gap=_gap("gap-1"), dry_run=True)
        assert outcome.queued is False
        assert batch.pending == []

    def test_the_same_gap_considered_twice_in_one_pass_is_queued_once(self) -> None:
        batch = bp.PromotionBatch()
        first = batch.consider(gap=_gap("gap-1"))
        second = batch.consider(gap=_gap("gap-1"))
        assert first.queued is True
        assert second.queued is False
        assert len(batch.pending) == 1

    def test_a_gap_covered_by_an_in_flight_batch_ticket_is_not_requeued(self) -> None:
        _batch_ticket(["gap-1"])

        second_batch = bp.PromotionBatch()
        outcome = second_batch.consider(gap=_gap("gap-1"))
        assert outcome.already_covered is True
        assert second_batch.pending == []

    def test_summary_names_what_was_turned_away(self) -> None:
        _batch_ticket(["gap-1"])
        batch = bp.PromotionBatch()
        batch.consider(gap=_gap("gap-1"))
        batch.consider(gap=_gap("gap-2"))
        with patch("teatree.loops.dream.umbrella_ledger.banned_terms_scanner.scan_text", return_value="banned"):
            batch.consider(gap=_gap("gap-3"))
        assert "1 already covered" in batch.summary
        assert "1 withheld" in batch.summary
        assert "collected 1 gap(s)" in batch.summary

    def test_no_collection_reports_an_empty_summary(self) -> None:
        assert bp.PromotionBatch().summary == ""

    def test_a_gap_already_pending_on_the_umbrella_is_not_requeued(self) -> None:
        _umbrella()
        bp.promote_batch(umbrella_url=UMBRELLA, batch=bp.PromotionBatch(pending=[_gap("gap-1")]))

        outcome = bp.PromotionBatch().consider(gap=_gap("gap-1"))

        assert (outcome.already_covered, outcome.queued) == (True, False)


class PromoteBatchTestCase(TestCase):
    """A pass queues its gaps on the umbrella host and mints nothing; zero pending queues nothing."""

    def test_an_empty_batch_queues_nothing(self) -> None:
        umbrella = _umbrella()
        outcome = bp.promote_batch(umbrella_url=UMBRELLA, batch=bp.PromotionBatch())
        umbrella.refresh_from_db()
        assert (outcome.queued, outcome.gap_count) == (False, 0)
        assert pending_entries(umbrella) == []

    def test_dry_run_writes_nothing(self) -> None:
        umbrella = _umbrella()
        batch = bp.PromotionBatch(pending=[_gap("gap-1")])
        outcome = bp.promote_batch(umbrella_url=UMBRELLA, batch=batch, dry_run=True)
        umbrella.refresh_from_db()
        assert outcome.queued is False
        assert pending_entries(umbrella) == []

    def test_many_collected_gaps_create_no_ticket_and_queue_every_gap(self) -> None:
        umbrella = _umbrella()
        tickets_before = Ticket.objects.count()
        batch = bp.PromotionBatch()
        for i in range(300):
            batch.consider(gap=_gap(f"gap-{i}"))

        outcome = bp.promote_batch(umbrella_url=UMBRELLA, batch=batch)

        umbrella.refresh_from_db()
        assert (outcome.queued, outcome.gap_count) == (True, 300)
        assert Ticket.objects.count() == tickets_before
        assert not Task.objects.exists()
        assert {entry["gap_key"] for entry in pending_entries(umbrella)} == {f"gap-{i}" for i in range(300)}

    def test_each_pending_entry_carries_its_title_and_citation_status(self) -> None:
        umbrella = _umbrella()
        _memory(key="cited").classify_core_gap()
        ConsolidatedMemory.objects.create(
            cluster_key="uncited", rule="r", source_files=[], member_count=1, max_member_weight=90
        )
        batch = bp.PromotionBatch(pending=[_gap(k, title=f"Fix {k}") for k in ("cited", "uncited", "no-row")])

        bp.promote_batch(umbrella_url=UMBRELLA, batch=batch)

        umbrella.refresh_from_db()
        assert [(e["gap_key"], e["title"], e["citation"]) for e in pending_entries(umbrella)] == [
            ("cited", "Fix cited", "cited"),
            ("uncited", "Fix uncited", "uncited"),
            ("no-row", "Fix no-row", "no-memory-row"),
        ]

    def test_a_queued_gap_leaves_the_drain_queue_on_a_promotion_anchor(self) -> None:
        _umbrella()
        _memory(key="gap-1").classify_core_gap()

        bp.promote_batch(umbrella_url=UMBRELLA, batch=bp.PromotionBatch(pending=[_gap("gap-1")]))

        row = ConsolidatedMemory.objects.get(cluster_key="gap-1")
        assert row.disposition == ConsolidatedMemory.Disposition.TICKETED
        assert is_promotion_anchor(row.ticket_url)
        assert row.ticket_url.startswith(f"{UMBRELLA}#")

    def test_with_no_umbrella_ticket_nothing_is_queued_and_the_gap_stays_in_the_drain_queue(self) -> None:
        _memory(key="gap-1").classify_core_gap()

        outcome = bp.promote_batch(umbrella_url=UMBRELLA, batch=bp.PromotionBatch(pending=[_gap("gap-1")]))

        assert outcome.queued is False
        assert UMBRELLA in outcome.reason
        assert not Ticket.objects.exists()
        assert [row.cluster_key for row in ConsolidatedMemory.objects.needs_ticket()] == ["gap-1"]

    def test_re_queueing_the_same_gaps_adds_no_duplicate(self) -> None:
        umbrella = _umbrella()
        batch = bp.PromotionBatch(pending=[_gap("gap-1"), _gap("gap-2")])
        bp.promote_batch(umbrella_url=UMBRELLA, batch=batch)
        bp.promote_batch(umbrella_url=UMBRELLA, batch=batch)
        umbrella.refresh_from_db()
        assert [entry["gap_key"] for entry in pending_entries(umbrella)] == ["gap-1", "gap-2"]

    def test_a_failed_stamp_rolls_the_queue_back(self) -> None:
        umbrella = _umbrella()
        _memory(key="gap-a").classify_core_gap()
        with (
            patch.object(ConsolidatedMemory, "mark_ticketed", side_effect=RuntimeError("db down")),
            pytest.raises(RuntimeError),
        ):
            bp.promote_batch(umbrella_url=UMBRELLA, batch=bp.PromotionBatch(pending=[_gap("gap-a")]))
        umbrella.refresh_from_db()
        assert pending_entries(umbrella) == []


LONG_RULE = (
    "A dream-batch manifest must carry each gap's full rule, grounding citation and durable destination, "
    "not only its truncated checkbox title. Otherwise the coder guesses what to fix."
)


def _promoted_context(*gaps: GapSpec) -> str:
    pending: list[DreamGapEntry] = [
        {"gap_key": gap.gap_key, "title": gap.title, "cluster_key": gap.cluster_key} for gap in gaps
    ]
    return BacklogSweepScanner(overlay_name="t3-teatree", dream_umbrella_url=UMBRELLA)._dream_gap_section(pending)


class BatchContextLedgerDetailTestCase(TestCase):
    """The sweep task carries each gap's full ledger row."""

    def test_the_context_carries_the_full_rule_the_title_cut_off(self) -> None:
        _memory(key="gap-1", rule=LONG_RULE)
        title = gap_title("Workflow gap", LONG_RULE)
        assert "durable destination" not in title

        context = _promoted_context(GapSpec(gap_key="gap-1", title=title, cluster_key="gap-1"))

        assert f"Rule: {LONG_RULE}" in context

    def test_the_context_carries_the_citation_and_the_destination(self) -> None:
        _memory(key="gap-1")

        context = _promoted_context(_gap("gap-1"))

        assert "Evidence: pushed without running the gate, CI went red" in context
        assert "Fix in: skills/ship/SKILL.md" in context

    def test_each_gaps_detail_sits_under_its_own_line(self) -> None:
        _memory(key="gap-a")
        _memory(key="gap-b")

        context = _promoted_context(_gap("gap-a"), _gap("gap-b"))

        rule_a = context.index("Rule: Run the tree-wide health gate before any push (gap-a).")
        rule_b = context.index("Rule: Run the tree-wide health gate before any push (gap-b).")
        assert context.index("- gap-a:") < rule_a < context.index("- gap-b:") < rule_b

    def test_the_detail_is_read_by_cluster_key_not_gap_key(self) -> None:
        _memory(key="compliance-gap", rule="The decoy row keyed by the gap key.")
        _memory(key="cluster-x", rule="The row keyed by the cluster key.")

        context = _promoted_context(GapSpec(gap_key="compliance-gap", title="Escalate x", cluster_key="cluster-x"))

        assert "Rule: The row keyed by the cluster key." in context
        assert "decoy" not in context

    def test_a_gap_with_no_ledger_row_keeps_its_title_line_and_no_empty_labels(self) -> None:
        key = "compliance-recurrence-x"

        context = _promoted_context(GapSpec(gap_key=key, title="Escalate x to a gate", cluster_key=key))

        assert f"- {key}:" in context
        assert "Rule:" not in context
        assert "Evidence:" not in context
        assert "Fix in:" not in context

    def test_a_blank_citation_and_destination_render_no_label(self) -> None:
        _memory(key="gap-1", verified_citation="", durable_destination="  ")

        context = _promoted_context(_gap("gap-1"))

        assert "Rule: Run the tree-wide health gate" in context
        assert "Evidence:" not in context
        assert "Fix in:" not in context

    def test_a_multi_line_rule_renders_on_one_line_with_every_word(self) -> None:
        _memory(key="gap-1", rule="First   clause of the rule.\n\n  Second clause\tstays too.")

        context = _promoted_context(_gap("gap-1"))

        assert "Rule: First clause of the rule. Second clause stays too." in context

    def test_the_public_umbrella_gets_only_the_short_title(self) -> None:
        _memory(key="gap-1", rule=LONG_RULE).classify_core_gap()
        umbrella = _umbrella()
        title = gap_title("Workflow gap", LONG_RULE)
        batch = bp.PromotionBatch(pending=[GapSpec(gap_key="gap-1", title=title, cluster_key="gap-1")])
        with patch.object(bp, "code_host_for", side_effect=AssertionError("public issue write")):
            assert bp.promote_batch(umbrella_url=UMBRELLA, batch=batch).queued

        umbrella.refresh_from_db()
        pending = pending_entries(umbrella)
        assert pending[0]["title"] == title
        assert LONG_RULE not in str(pending)
        assert LONG_RULE not in title
        assert f"Rule: {LONG_RULE}" in _promoted_context(batch.pending[0])

    def test_the_ledger_is_read_in_one_query_for_the_whole_batch(self) -> None:
        for key in ("gap-a", "gap-b", "gap-c"):
            _memory(key=key)
        gaps = [_gap("gap-a"), _gap("gap-b"), _gap("gap-c")]

        with self.assertNumQueries(1):
            context = _promoted_context(*gaps)

        assert context.count("Rule: ") == 3


class EvalArtifactFoldTestCase(TestCase):
    def test_scenario_payload_reaches_the_coding_tickets_context(self) -> None:
        umbrella = _umbrella()
        detail = "evals/scenarios/promoted_drift.yaml:\n```yaml\n- name: candidate\n```"
        gap = GapSpec(
            gap_key="eval-scenario-candidate",
            title="Add validated eval",
            cluster_key="eval-scenario-candidate",
            detail=detail,
        )
        bp.promote_batch(umbrella_url=UMBRELLA, batch=bp.PromotionBatch(pending=[gap]))
        host = Ticket.objects.create(
            issue_url="https://github.com/souliane/teatree/issues/56",
            role=Ticket.Role.AUTHOR,
            state=Ticket.State.PLAN_RECORDED,
        )
        record_test_plan(host)

        outcome = attach_dream_gaps(host, [{"gap_key": gap.gap_key}], umbrella=umbrella, code_host=_fake_host())

        host.refresh_from_db()
        assert outcome.attached == [gap.gap_key]
        assert detail in host.context
        assert pending_entries(umbrella) == []


class GapCoveredTestCase(TestCase):
    """A gap is covered while pending, in flight or delivered; never while dropped or by an IGNORED row."""

    def test_no_batch_ticket_means_not_covered(self) -> None:
        assert bp.gap_covered("gap-1") is False

    def test_a_pending_gap_is_covered_by_the_umbrella_host(self) -> None:
        umbrella = _umbrella()
        bp.promote_batch(umbrella_url=UMBRELLA, batch=bp.PromotionBatch(pending=[_gap("gap-1")]))
        assert bp.covering_ticket("gap-1") == umbrella

    def test_an_in_flight_batch_ticket_covers_its_gaps(self) -> None:
        _batch_ticket(["gap-1"])
        assert bp.gap_covered("gap-1") is True

    def test_an_open_host_the_sweep_folded_into_covers_its_gaps(self) -> None:
        host = Ticket.objects.create(
            issue_url="https://github.com/souliane/teatree/issues/56",
            extra={"dream_gap_batch": [{"gap_key": "gap-1", "cluster_key": "gap-1"}]},
        )
        assert bp.covering_ticket("gap-1") == host

    def test_an_ignored_batch_ticket_never_covers(self) -> None:
        ticket = _batch_ticket(["gap-1"])
        ticket.state = Ticket.State.IGNORED
        ticket.save()
        assert bp.gap_covered("gap-1") is False

    def test_a_reconciled_delivered_gap_is_covered(self) -> None:
        ticket = _batch_ticket(["gap-1"])
        ticket.merge_extra(
            set_keys={"dream_gap_reconciled_at": "2026-01-01T00:00:00", "dream_gap_claimed_delivered": ["gap-1"]}
        )
        assert bp.gap_covered("gap-1") is True

    def test_a_reconciled_dropped_gap_is_not_covered(self) -> None:
        ticket = _batch_ticket(["gap-1"])
        ticket.merge_extra(
            set_keys={"dream_gap_reconciled_at": "2026-01-01T00:00:00", "dream_gap_claimed_delivered": []}
        )
        assert bp.gap_covered("gap-1") is False

    def test_a_gap_dropped_by_one_ticket_but_covered_by_a_later_in_flight_ticket_is_covered(self) -> None:
        # #4776 follow-up: a gap dropped by an old reconciled ticket is re-offered and
        # picked up by a NEW batch, so both tickets end up listing the same gap key —
        # the scan must not stop at the first (stale, dropped) match.
        old_ticket = _batch_ticket(["gap-1"], suffix="old")
        old_ticket.merge_extra(
            set_keys={"dream_gap_reconciled_at": "2026-01-01T00:00:00", "dream_gap_claimed_delivered": []}
        )
        assert bp.gap_covered("gap-1") is False

        _batch_ticket(["gap-1", "gap-2"], suffix="new")

        assert bp.gap_covered("gap-1") is True


class ReconcileBatchesTestCase(TestCase):
    """Only DELIVERED gaps of a MERGED batch ticket get checked + retired."""

    def _merged_batch_ticket(self, *, keys: list[str]) -> Ticket:
        for key in keys:
            _memory(key=key)
        ticket = _batch_ticket(keys)
        ticket.pull_requests.create(
            url="https://github.com/souliane/teatree/pull/9100", repo=REPO, iid="9100", state="merged"
        )
        ticket.state = Ticket.State.MERGED
        ticket.save()
        return ticket

    def test_only_the_delivered_gap_is_checked_and_retired(self) -> None:
        ticket = self._merged_batch_ticket(keys=["gap-a", "gap-b"])
        ticket.merge_extra(set_keys={"dream_gap_claimed_delivered": ["gap-a"]})
        host = _host_with_gap_a_and_b()

        reconciled = bp.reconcile_batches(host, umbrella_url=UMBRELLA)

        assert reconciled == [ticket]
        body = host.get_issue(UMBRELLA)["body"]
        assert "- [x] Fix the gate gap-a <!-- dream-gap gap-a -->" in body
        assert "- [ ] Fix the gate gap-b <!-- dream-gap gap-b -->" in body
        assert _retired("gap-a")
        assert not _retired("gap-b")

    def test_the_ticket_is_stamped_reconciled_regardless_of_partial_delivery(self) -> None:
        ticket = self._merged_batch_ticket(keys=["gap-a", "gap-b"])
        ticket.merge_extra(set_keys={"dream_gap_claimed_delivered": ["gap-a"]})
        host = _host_with_gap_a_and_b()
        bp.reconcile_batches(host, umbrella_url=UMBRELLA)
        ticket.refresh_from_db()
        assert ticket.extra.get("dream_gap_reconciled_at")

    def test_a_dropped_gap_is_re_offered_by_the_next_passs_consider(self) -> None:
        self._merged_batch_ticket(keys=["gap-a", "gap-b"])
        ticket = Ticket.objects.exclude(extra__dream_gap_batch__isnull=True).get()
        ticket.merge_extra(set_keys={"dream_gap_claimed_delivered": ["gap-a"]})
        host = _host_with_gap_a_and_b()
        bp.reconcile_batches(host, umbrella_url=UMBRELLA)

        assert bp.gap_covered("gap-b") is False
        next_batch = bp.PromotionBatch()
        outcome = next_batch.consider(gap=_gap("gap-b"))
        assert outcome.already_covered is False
        assert outcome.queued is True

    def test_an_unmerged_ticket_is_left_alone(self) -> None:
        _memory(key="gap-a")
        _batch_ticket(["gap-a"])
        assert bp.reconcile_batches(_fake_host(), umbrella_url=UMBRELLA) == []

    def test_a_merged_sweep_host_reconciles_its_delivered_gap_with_no_umbrella_checkbox(self) -> None:
        _memory(key="gap-a").classify_core_gap()
        _stamp_memory_promoted("gap-a", anchor_url=f"{UMBRELLA}#dream-batch=q")
        host_ticket = Ticket.objects.create(
            issue_url="https://github.com/souliane/teatree/issues/56",
            state=Ticket.State.MERGED,
            extra={
                "dream_gap_batch": [{"gap_key": "gap-a", "cluster_key": "gap-a"}],
                "dream_gap_claimed_delivered": ["gap-a"],
            },
        )
        forge = _fake_host(body="nothing about this gap\n")

        assert bp.reconcile_batches(forge, umbrella_url=UMBRELLA) == [host_ticket]

        forge.update_issue.assert_not_called()
        assert _retired("gap-a")

    def test_binding_memory_is_never_retired_even_when_delivered(self) -> None:
        ticket = self._merged_batch_ticket(keys=["gap-a"])
        ConsolidatedMemory.objects.filter(cluster_key="gap-a").update(is_binding=True)
        ticket.merge_extra(set_keys={"dream_gap_claimed_delivered": ["gap-a"]})
        host = _host_with_gap_a_and_b()
        bp.reconcile_batches(host, umbrella_url=UMBRELLA)
        assert not _retired("gap-a")

    def test_a_claim_outside_the_manifest_is_ignored(self) -> None:
        ticket = self._merged_batch_ticket(keys=["gap-a"])
        ticket.merge_extra(set_keys={"dream_gap_claimed_delivered": ["gap-a", "not-in-manifest"]})
        host = _host_with_gap_a_and_b()
        # Must not raise (KeyError) on the untrusted key, and must still retire gap-a.
        bp.reconcile_batches(host, umbrella_url=UMBRELLA)
        assert _retired("gap-a")

    def test_an_unconfirmable_checkbox_write_defers_the_whole_ticket(self) -> None:
        ticket = self._merged_batch_ticket(keys=["gap-a", "gap-b"])
        ticket.merge_extra(set_keys={"dream_gap_claimed_delivered": ["gap-a", "gap-b"]})
        host = _host_with_gap_a_and_b()
        # The reconcile checks boxes through umbrella_ledger._ensure_gap_checked, which reads it there.
        scrubbed_update = "teatree.loops.dream.umbrella_ledger._scrubbed_update"
        with patch(scrubbed_update, return_value=False):  # patch-binding: defining-module
            assert bp.reconcile_batches(host, umbrella_url=UMBRELLA) == []
        ticket.refresh_from_db()
        assert not ticket.extra.get("dream_gap_reconciled_at")
        assert not _retired("gap-a")


class CoveringTicketTestCase(TestCase):
    """``covering_ticket`` names the ticket ``gap_covered`` answers from."""

    def test_an_uncovered_gap_has_no_covering_ticket(self) -> None:
        assert bp.covering_ticket("gap-1") is None

    def test_the_in_flight_ticket_wins_over_a_stale_dropped_one(self) -> None:
        dropped = _batch_ticket(["gap-1"], suffix="old")
        dropped.merge_extra(
            set_keys={"dream_gap_reconciled_at": "2026-01-01T00:00:00", "dream_gap_claimed_delivered": []}
        )
        in_flight = _batch_ticket(["gap-1", "gap-2"], suffix="new")

        assert bp.covering_ticket("gap-1") == in_flight


class StampedBatchReconcileTestCase(TestCase):
    """Rows stamped at promotion still retire on delivery, and a dropped gap is re-queued."""

    PR_URL = "https://github.com/souliane/teatree/pull/9100"

    def _stamped_merged_ticket(
        self, *, delivered: list[str], keys: tuple[str, ...] = ("gap-a", "gap-b"), with_pr: bool = True
    ) -> Ticket:
        for key in keys:
            _memory(key=key).classify_core_gap()
        ticket = _batch_ticket(keys)
        if with_pr:
            ticket.pull_requests.create(url=self.PR_URL, repo=REPO, iid="9100", state="merged")
        ticket.state = Ticket.State.MERGED
        ticket.save()
        ticket.merge_extra(set_keys={"dream_gap_claimed_delivered": delivered})
        return ticket

    def test_the_delivered_gap_retires_against_the_merged_pr(self) -> None:
        self._stamped_merged_ticket(delivered=["gap-a"])

        bp.reconcile_batches(_host_with_gap_a_and_b(), umbrella_url=UMBRELLA)

        row = ConsolidatedMemory.objects.get(cluster_key="gap-a")
        assert row.disposition == ConsolidatedMemory.Disposition.RESOLVED_RETIRED
        assert row.ticket_url == self.PR_URL

    def test_a_dropped_gap_is_reopened_and_queued_by_the_next_pass(self) -> None:
        self._stamped_merged_ticket(delivered=["gap-a"])

        bp.reconcile_batches(_host_with_gap_a_and_b(), umbrella_url=UMBRELLA)

        row = ConsolidatedMemory.objects.get(cluster_key="gap-b")
        assert row.disposition == ConsolidatedMemory.Disposition.CORE_GAP_NEEDS_TICKET
        assert row.ticket_url == ""
        next_batch = bp.PromotionBatch()
        outcomes = file_core_gap_tickets(umbrella_url=UMBRELLA, batch=next_batch)
        assert [outcome.filed for outcome in outcomes] == [True]
        assert [gap.gap_key for gap in next_batch.pending] == ["gap-b"]

    def test_a_row_a_newer_batch_owns_is_not_reopened(self) -> None:
        self._stamped_merged_ticket(delivered=["gap-a"])
        newer = _batch_ticket(["gap-b"], suffix="newer").issue_url
        ConsolidatedMemory.objects.get(cluster_key="gap-b").mark_ticketed(newer)

        bp.reconcile_batches(_host_with_gap_a_and_b(), umbrella_url=UMBRELLA)

        row = ConsolidatedMemory.objects.get(cluster_key="gap-b")
        assert row.disposition == ConsolidatedMemory.Disposition.TICKETED
        assert row.ticket_url == newer

    def _re_promote(self) -> bp.BatchOutcome:
        umbrella = _umbrella()
        batch = bp.PromotionBatch()
        file_core_gap_tickets(umbrella_url=UMBRELLA, batch=batch)
        outcome = bp.promote_batch(umbrella_url=UMBRELLA, batch=batch)
        assert bp.covering_ticket(batch.pending[0].gap_key) == umbrella
        return outcome

    def _assert_queued_on_the_umbrella(self, keys: tuple[str, ...]) -> None:
        umbrella = _umbrella()
        assert {entry["gap_key"] for entry in pending_entries(umbrella)} == set(keys)
        assert not Task.objects.exists()
        for key in keys:
            assert ConsolidatedMemory.objects.get(cluster_key=key).ticket_url.startswith(f"{UMBRELLA}#dream-batch=")

    def test_a_dropped_single_gap_is_re_queued_on_the_umbrella(self) -> None:
        self._stamped_merged_ticket(delivered=[], keys=("gap-a",))
        bp.reconcile_batches(_host_with_gap_a_and_b(), umbrella_url=UMBRELLA)

        assert self._re_promote().queued is True
        self._assert_queued_on_the_umbrella(("gap-a",))

    def test_a_fresh_generation_reads_the_ledger_once_and_carries_the_detail(self) -> None:
        self._stamped_merged_ticket(delivered=[], keys=("gap-a",))
        bp.reconcile_batches(_host_with_gap_a_and_b(), umbrella_url=UMBRELLA)
        assert self._re_promote().queued is True

        umbrella = _umbrella()
        pending = pending_entries(umbrella)
        with self.assertNumQueries(1):
            context = BacklogSweepScanner(overlay_name="t3-teatree", dream_umbrella_url=UMBRELLA)._dream_gap_section(
                pending
            )

        assert "Rule: Run the tree-wide health gate before any push (gap-a)." in context
        assert "Evidence: pushed without running the gate, CI went red" in context
        assert "Fix in: skills/ship/SKILL.md" in context

    def test_a_wholly_dropped_batch_is_re_queued_on_the_umbrella(self) -> None:
        self._stamped_merged_ticket(delivered=[])
        bp.reconcile_batches(_host_with_gap_a_and_b(), umbrella_url=UMBRELLA)

        assert self._re_promote().queued is True
        self._assert_queued_on_the_umbrella(("gap-a", "gap-b"))
        assert not ConsolidatedMemory.objects.needs_ticket().exists()

    def test_a_delivered_gap_on_a_merged_ticket_without_a_pr_row_retires_against_the_ticket(self) -> None:
        ticket = self._stamped_merged_ticket(delivered=["gap-a"], with_pr=False)

        bp.reconcile_batches(_host_with_gap_a_and_b(), umbrella_url=UMBRELLA)

        row = ConsolidatedMemory.objects.get(cluster_key="gap-a")
        assert row.disposition == ConsolidatedMemory.Disposition.RESOLVED_RETIRED
        assert row.archive_path == ticket.issue_url

    def _partially_confirmed_reconcile(self, memory_dir: Path, *, with_pr: bool) -> dict[str, Path]:
        keys = ("gap-a", "gap-b", "gap-c")
        sources = {key: memory_dir / f"feedback_{key}.md" for key in keys}
        for key, source in sources.items():
            source.write_text("the lesson")
            _memory(key=key, source_files=[str(source)]).classify_core_gap()
        ticket = _batch_ticket(keys)
        if with_pr:
            ticket.pull_requests.create(url=self.PR_URL, repo=REPO, iid="9100", state="merged")
        ticket.state = Ticket.State.MERGED
        ticket.save()
        ticket.merge_extra(set_keys={"dream_gap_claimed_delivered": ["gap-a", "gap-c"]})
        with patch("teatree.memory_audit.discover_memory_dirs", return_value=[memory_dir]):
            bp.reconcile_batches(_host_with_gap_a_and_b(), umbrella_url=UMBRELLA)
        return sources

    def test_a_dropped_gap_survives_a_partial_reconcile_on_the_ticket_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "memory").mkdir()
            sources = self._partially_confirmed_reconcile(Path(tmp) / "memory", with_pr=False)

            for key in ("gap-b", "gap-a"):
                row = ConsolidatedMemory.objects.get(cluster_key=key)
                assert (key, row.disposition) == (key, ConsolidatedMemory.Disposition.TICKETED)
                assert sources[key].exists()

    def test_a_dropped_gap_survives_a_partial_reconcile_on_a_merged_pr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "memory").mkdir()
            sources = self._partially_confirmed_reconcile(Path(tmp) / "memory", with_pr=True)

            row = ConsolidatedMemory.objects.get(cluster_key="gap-b")
            assert row.disposition == ConsolidatedMemory.Disposition.TICKETED
            assert sources["gap-b"].exists()


class SweepHostReconcileTestCase(TestCase):
    """A gap a merged sweep host neither delivered nor rejected goes back to the drain (verdict 586)."""

    HOST_URL = "https://github.com/souliane/teatree/issues/56"

    def _merged_host_with(self, *, delivered: list[str], rejected: list[str]) -> Ticket:
        umbrella = _umbrella()
        for key in ("gap-a", "gap-b", "gap-c"):
            _memory(key=key).classify_core_gap()
        bp.promote_batch(
            umbrella_url=UMBRELLA, batch=bp.PromotionBatch(pending=[_gap(k) for k in ("gap-a", "gap-b", "gap-c")])
        )
        host = Ticket.objects.create(issue_url=self.HOST_URL, role=Ticket.Role.AUTHOR, state=Ticket.State.PLAN_RECORDED)
        record_test_plan(host)
        keys = [{"gap_key": key} for key in ("gap-a", "gap-b", "gap-c")]
        assert attach_dream_gaps(host, keys, umbrella=umbrella, code_host=_fake_host()).attached
        for key in delivered:
            record_gap_disposition(host, key, citation=f"fixed {key}")
        for key in rejected:
            record_gap_disposition(host, key, rejection=f"not a core gap: {key}")
        host.state = Ticket.State.MERGED
        host.save()
        return host

    def test_an_undelivered_gap_is_reopened_and_re_queued_by_the_next_pass(self) -> None:
        self._merged_host_with(delivered=["gap-a"], rejected=["gap-c"])

        bp.reconcile_batches(_fake_host(), umbrella_url=UMBRELLA)

        row_b = ConsolidatedMemory.objects.get(cluster_key="gap-b")
        assert (row_b.disposition, row_b.ticket_url) == (ConsolidatedMemory.Disposition.CORE_GAP_NEEDS_TICKET, "")
        next_batch = bp.PromotionBatch()
        file_core_gap_tickets(umbrella_url=UMBRELLA, batch=next_batch)
        assert [gap.gap_key for gap in next_batch.pending] == ["gap-b"]

    def test_a_rejected_gap_is_not_reopened_and_stays_owned(self) -> None:
        host = self._merged_host_with(delivered=["gap-a"], rejected=["gap-c"])

        bp.reconcile_batches(_fake_host(), umbrella_url=UMBRELLA)

        row_c = ConsolidatedMemory.objects.get(cluster_key="gap-c")
        assert row_c.disposition == ConsolidatedMemory.Disposition.TICKETED
        assert bp.covering_ticket("gap-c") == host

    def test_the_delivered_gap_retires(self) -> None:
        self._merged_host_with(delivered=["gap-a"], rejected=[])

        bp.reconcile_batches(_fake_host(), umbrella_url=UMBRELLA)

        assert _retired("gap-a")


class LegacyBatchKeepsItsOwnUmbrellaTestCase(TestCase):
    """A pre-sweep batch ticket checks its box on the umbrella it recorded, not the configured one."""

    OWN_UMBRELLA = "https://github.com/souliane/teatree/issues/2663"
    CONFIGURED = "https://gitlab.com/o/factory/-/work_items/249"

    def test_the_checkbox_is_checked_on_the_tickets_own_umbrella(self) -> None:
        _memory(key="gap-a").classify_core_gap()
        ticket = _batch_ticket(["gap-a"])
        ticket.state = Ticket.State.MERGED
        ticket.save()
        ticket.merge_extra(set_keys={"dream_gap_claimed_delivered": ["gap-a"]})
        own_forge = _host_with_gap_a_and_b()
        configured_forge = _fake_host(body="nothing here\n")

        with patch("teatree.loops.dream.batch_promote.code_host_for", return_value=own_forge) as resolve:
            reconciled = bp.reconcile_batches(configured_forge, umbrella_url=self.CONFIGURED)

        assert reconciled == [ticket]
        resolve.assert_called_with(self.OWN_UMBRELLA)
        assert "- [x] Fix the gate gap-a" in own_forge.get_issue(self.OWN_UMBRELLA)["body"]
        configured_forge.update_issue.assert_not_called()
        assert _retired("gap-a")
