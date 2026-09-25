"""One dream pass's promotions become ONE ticket and ONE PR, never one per gap (#4776).

``PromotionBatch`` replaces the deleted ``PromotionBudget``/``promotion_cap`` ration:
every promoting phase collects its gaps via ``consider()`` and ``promote_batch()``
mints AT MOST ONE ticket for the whole pass, once every phase has run. These tests
drive that flow with a STATEFUL fake code host and real ``Ticket`` / ``ConsolidatedMemory``
rows, mirroring ``test_umbrella_ledger.py``'s fixtures.
"""

from unittest.mock import MagicMock, patch

from django.test import TestCase

from teatree.core.backend_protocols import CodeHostBackend
from teatree.core.models import ConsolidatedMemory
from teatree.core.models.ticket import Ticket
from teatree.loops.dream import batch_promote as bp
from teatree.loops.dream.promote_memory import file_core_gap_tickets
from teatree.loops.dream.umbrella_ledger import GapSpec

UMBRELLA = "https://github.com/souliane/teatree/issues/2663"
REPO = "souliane/teatree"


def _fake_host(*, body: str = "## Open gaps\n") -> CodeHostBackend:
    """A STATEFUL umbrella: its body persists across writes, as the real issue does."""
    state = {"body": body}

    def _update(**kwargs: object) -> dict[str, int]:
        state["body"] = str(kwargs["body"])
        return {"number": 2663}

    host = MagicMock(spec=CodeHostBackend)
    host.get_issue.side_effect = lambda *_a, **_k: {"body": state["body"]}
    host.update_issue.side_effect = _update
    return host


def _gap(key: str, *, title: str | None = None) -> GapSpec:
    return GapSpec(gap_key=key, title=title or f"Fix the gate {key}", cluster_key=key)


def _memory(*, key: str = "gap-1", binding: bool = False, source_files: list | None = None) -> ConsolidatedMemory:
    return ConsolidatedMemory.objects.create(
        cluster_key=key,
        rule=f"Run the tree-wide health gate before any push ({key}).",
        source_files=source_files if source_files is not None else [f"feedback_{key}.md"],
        durable_destination="skills/ship/SKILL.md",
        is_binding=binding,
        member_count=1,
        max_member_weight=90,
        verified_citation="pushed without running the gate, CI went red",
    )


def _host_with_gap_a_and_b() -> CodeHostBackend:
    body = (
        "## Open gaps\n"
        + "\n".join(f"- [ ] Fix the gate {k} <!-- dream-gap {k} -->" for k in ["gap-a", "gap-b"])
        + "\n"
    )
    host = MagicMock(spec=CodeHostBackend)
    state: dict[str, str] = {"body": body}

    def _update(**kwargs: object) -> dict[str, int]:
        state["body"] = str(kwargs["body"])
        return {"number": 2663}

    host.get_issue.side_effect = lambda *_a, **_k: {"body": state["body"], "state": "merged"}
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

    def test_a_gap_covered_by_a_legacy_in_flight_ticket_is_not_requeued(self) -> None:
        # The OLD per-gap scheme's Ticket rows keep draining via
        # umbrella_ledger.reconcile_merged_gaps; a gap already scheduled that way must
        # never be duplicated into a NEW batch.
        Ticket.objects.create(
            issue_url=f"{UMBRELLA}#dream-gap=gap-1",
            role=Ticket.Role.AUTHOR,
            extra={"dream_gap_key": "gap-1", "dream_memory_cluster_key": "gap-1", "dream_umbrella_url": UMBRELLA},
        )
        batch = bp.PromotionBatch()
        outcome = batch.consider(gap=_gap("gap-1"))
        assert outcome.already_covered is True
        assert outcome.queued is False
        assert batch.pending == []
        assert batch.already_covered == 1

    def test_a_reconciled_legacy_ticket_no_longer_covers_its_gap(self) -> None:
        Ticket.objects.create(
            issue_url=f"{UMBRELLA}#dream-gap=gap-1",
            role=Ticket.Role.AUTHOR,
            extra={
                "dream_gap_key": "gap-1",
                "dream_memory_cluster_key": "gap-1",
                "dream_umbrella_url": UMBRELLA,
                "dream_gap_reconciled_at": "2026-01-01T00:00:00",
            },
        )
        batch = bp.PromotionBatch()
        outcome = batch.consider(gap=_gap("gap-1"))
        assert outcome.already_covered is False
        assert outcome.queued is True

    def test_a_gap_covered_by_an_in_flight_batch_ticket_is_not_requeued(self) -> None:
        first_batch = bp.PromotionBatch()
        host = _fake_host()
        first_batch.consider(gap=_gap("gap-1"))
        bp.promote_batch(host, umbrella_url=UMBRELLA, batch=first_batch)

        second_batch = bp.PromotionBatch()
        outcome = second_batch.consider(gap=_gap("gap-1"))
        assert outcome.already_covered is True
        assert second_batch.pending == []

    def test_summary_names_what_was_turned_away(self) -> None:
        Ticket.objects.create(
            issue_url=f"{UMBRELLA}#dream-gap=gap-1",
            role=Ticket.Role.AUTHOR,
            extra={"dream_gap_key": "gap-1", "dream_memory_cluster_key": "gap-1", "dream_umbrella_url": UMBRELLA},
        )
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


class PromoteBatchTestCase(TestCase):
    """The required negative control: zero pending gaps mints zero tickets."""

    def test_an_empty_batch_mints_no_ticket(self) -> None:
        outcome = bp.promote_batch(_fake_host(), umbrella_url=UMBRELLA, batch=bp.PromotionBatch())
        assert outcome.scheduled is False
        assert outcome.gap_count == 0
        assert Ticket.objects.exclude(extra__dream_gap_batch__isnull=True).count() == 0

    def test_dry_run_writes_and_schedules_nothing(self) -> None:
        batch = bp.PromotionBatch(pending=[_gap("gap-1")])
        host = _fake_host()
        outcome = bp.promote_batch(host, umbrella_url=UMBRELLA, batch=batch, dry_run=True)
        assert outcome.scheduled is False
        host.update_issue.assert_not_called()
        assert Ticket.objects.exclude(extra__dream_gap_batch__isnull=True).count() == 0

    def test_many_pending_gaps_collapse_into_exactly_one_ticket(self) -> None:
        # The acceptance criterion, literally: 300 pending gaps schedule ONE task, not
        # 300 and not a capped 5 — there is nothing left to ration (#4776).
        host = _fake_host()
        batch = bp.PromotionBatch()
        for i in range(300):
            batch.consider(gap=_gap(f"gap-{i}"))
        outcome = bp.promote_batch(host, umbrella_url=UMBRELLA, batch=batch)
        assert outcome.scheduled is True
        assert outcome.gap_count == 300
        batch_tickets = Ticket.objects.exclude(extra__dream_gap_batch__isnull=True)
        assert batch_tickets.count() == 1
        manifest = batch_tickets.first().extra["dream_gap_batch"]
        assert {entry["gap_key"] for entry in manifest} == {f"gap-{i}" for i in range(300)}
        # ONE forge read + ONE forge write for the whole batch, not one pair per gap.
        assert host.get_issue.call_count == 1
        assert host.update_issue.call_count == 1

    def test_every_pending_gap_gets_an_umbrella_checkbox(self) -> None:
        host = _fake_host()
        batch = bp.PromotionBatch(pending=[_gap("gap-1"), _gap("gap-2")])
        bp.promote_batch(host, umbrella_url=UMBRELLA, batch=batch)
        body = host.get_issue(UMBRELLA)["body"]
        assert "<!-- dream-gap gap-1 -->" in body
        assert "<!-- dream-gap gap-2 -->" in body

    def test_the_ticket_context_carries_the_manifest(self) -> None:
        host = _fake_host()
        batch = bp.PromotionBatch(pending=[_gap("gap-1", title="Fix the widget")])
        bp.promote_batch(host, umbrella_url=UMBRELLA, batch=batch)
        ticket = Ticket.objects.exclude(extra__dream_gap_batch__isnull=True).get()
        assert "gap-1" in ticket.context
        assert "Fix the widget" in ticket.context
        assert ticket.role == Ticket.Role.AUTHOR
        assert ticket.extra["dream_umbrella_url"] == UMBRELLA

    def test_re_running_the_same_pending_set_reuses_the_same_ticket(self) -> None:
        # Idempotent: a retried pass (or a pass that raised right after minting) must
        # not mint a second ticket for the identical pending set.
        host = _fake_host()
        batch = bp.PromotionBatch(pending=[_gap("gap-1"), _gap("gap-2")])
        bp.promote_batch(host, umbrella_url=UMBRELLA, batch=batch)
        bp.promote_batch(host, umbrella_url=UMBRELLA, batch=batch)
        assert Ticket.objects.exclude(extra__dream_gap_batch__isnull=True).count() == 1


class GapCoveredTestCase(TestCase):
    """A gap is covered while in flight, or delivered; never while dropped."""

    def test_no_batch_ticket_means_not_covered(self) -> None:
        assert bp.gap_covered("gap-1") is False

    def test_an_in_flight_batch_ticket_covers_its_gaps(self) -> None:
        host = _fake_host()
        batch = bp.PromotionBatch(pending=[_gap("gap-1")])
        bp.promote_batch(host, umbrella_url=UMBRELLA, batch=batch)
        assert bp.gap_covered("gap-1") is True

    def test_a_reconciled_delivered_gap_is_covered(self) -> None:
        host = _fake_host()
        batch = bp.PromotionBatch(pending=[_gap("gap-1")])
        bp.promote_batch(host, umbrella_url=UMBRELLA, batch=batch)
        ticket = Ticket.objects.exclude(extra__dream_gap_batch__isnull=True).get()
        ticket.merge_extra(
            set_keys={"dream_gap_reconciled_at": "2026-01-01T00:00:00", "dream_gap_claimed_delivered": ["gap-1"]}
        )
        assert bp.gap_covered("gap-1") is True

    def test_a_reconciled_dropped_gap_is_not_covered(self) -> None:
        host = _fake_host()
        batch = bp.PromotionBatch(pending=[_gap("gap-1")])
        bp.promote_batch(host, umbrella_url=UMBRELLA, batch=batch)
        ticket = Ticket.objects.exclude(extra__dream_gap_batch__isnull=True).get()
        ticket.merge_extra(
            set_keys={"dream_gap_reconciled_at": "2026-01-01T00:00:00", "dream_gap_claimed_delivered": []}
        )
        assert bp.gap_covered("gap-1") is False

    def test_a_gap_dropped_by_one_ticket_but_covered_by_a_later_in_flight_ticket_is_covered(self) -> None:
        # #4776 follow-up: a gap dropped by an old reconciled ticket is re-offered and
        # picked up by a NEW batch, so both tickets end up listing the same gap key —
        # the scan must not stop at the first (stale, dropped) match.
        host = _fake_host()
        dropped = bp.PromotionBatch(pending=[_gap("gap-1")])
        bp.promote_batch(host, umbrella_url=UMBRELLA, batch=dropped)
        old_ticket = Ticket.objects.exclude(extra__dream_gap_batch__isnull=True).get()
        old_ticket.merge_extra(
            set_keys={"dream_gap_reconciled_at": "2026-01-01T00:00:00", "dream_gap_claimed_delivered": []}
        )
        assert bp.gap_covered("gap-1") is False

        re_offered = bp.PromotionBatch(pending=[_gap("gap-1"), _gap("gap-2")])
        bp.promote_batch(host, umbrella_url=UMBRELLA, batch=re_offered)
        assert Ticket.objects.exclude(extra__dream_gap_batch__isnull=True).count() == 2

        assert bp.gap_covered("gap-1") is True


class ReconcileBatchesTestCase(TestCase):
    """Only DELIVERED gaps of a MERGED batch ticket get checked + retired."""

    def _merged_batch_ticket(self, *, keys: list[str]) -> Ticket:
        for key in keys:
            _memory(key=key)
        host = _fake_host()
        batch = bp.PromotionBatch(pending=[_gap(key) for key in keys])
        bp.promote_batch(host, umbrella_url=UMBRELLA, batch=batch)
        ticket = Ticket.objects.exclude(extra__dream_gap_batch__isnull=True).get()
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
        host = _fake_host()
        batch = bp.PromotionBatch(pending=[_gap("gap-a")])
        bp.promote_batch(host, umbrella_url=UMBRELLA, batch=batch)
        assert bp.reconcile_batches(host, umbrella_url=UMBRELLA) == []

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
        with patch("teatree.loops.dream.umbrella_ledger._scrubbed_update", return_value=False):
            assert bp.reconcile_batches(host, umbrella_url=UMBRELLA) == []
        ticket.refresh_from_db()
        assert not ticket.extra.get("dream_gap_reconciled_at")
        assert not _retired("gap-a")


class CoveringTicketTestCase(TestCase):
    """``covering_ticket`` names the ticket ``gap_covered`` answers from."""

    def test_an_uncovered_gap_has_no_covering_ticket(self) -> None:
        assert bp.covering_ticket("gap-1") is None

    def test_the_in_flight_ticket_wins_over_a_stale_dropped_one(self) -> None:
        host = _fake_host()
        bp.promote_batch(host, umbrella_url=UMBRELLA, batch=bp.PromotionBatch(pending=[_gap("gap-1")]))
        dropped = Ticket.objects.exclude(extra__dream_gap_batch__isnull=True).get()
        dropped.merge_extra(
            set_keys={"dream_gap_reconciled_at": "2026-01-01T00:00:00", "dream_gap_claimed_delivered": []}
        )
        bp.promote_batch(host, umbrella_url=UMBRELLA, batch=bp.PromotionBatch(pending=[_gap("gap-1"), _gap("gap-2")]))
        in_flight = Ticket.objects.exclude(extra__dream_gap_batch__isnull=True).exclude(pk=dropped.pk).get()

        assert bp.covering_ticket("gap-1") == in_flight


class StampedBatchReconcileTestCase(TestCase):
    """Rows stamped at promotion still retire on delivery, and a dropped gap is re-queued."""

    PR_URL = "https://github.com/souliane/teatree/pull/9100"

    def _stamped_merged_ticket(self, *, delivered: list[str]) -> Ticket:
        for key in ("gap-a", "gap-b"):
            _memory(key=key).classify_core_gap()
        batch = bp.PromotionBatch(pending=[_gap("gap-a"), _gap("gap-b")])
        bp.promote_batch(_fake_host(), umbrella_url=UMBRELLA, batch=batch)
        ticket = Ticket.objects.exclude(extra__dream_gap_batch__isnull=True).get()
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

    def test_a_row_restamped_by_a_newer_batch_is_not_reopened(self) -> None:
        self._stamped_merged_ticket(delivered=["gap-a"])
        newer = f"{UMBRELLA}#dream-batch=newer"
        ConsolidatedMemory.objects.get(cluster_key="gap-b").mark_ticketed(newer)

        bp.reconcile_batches(_host_with_gap_a_and_b(), umbrella_url=UMBRELLA)

        row = ConsolidatedMemory.objects.get(cluster_key="gap-b")
        assert row.disposition == ConsolidatedMemory.Disposition.TICKETED
        assert row.ticket_url == newer
