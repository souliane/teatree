import pytest
from django.test import TestCase

from teatree.core.models import ConsolidatedMemory, Ticket
from teatree.core.models.dream_gap_ledger import (
    ADDRESS,
    REJECT,
    DreamGapLedgerError,
    pending_entries,
    queue_pending,
    record_gap_disposition,
    take_pending,
    undispositioned_gap_keys,
)


def _host(*keys: str) -> Ticket:
    return Ticket.objects.create(
        overlay="test",
        issue_url="https://github.com/o/r/issues/56",
        extra={"dream_gap_batch": [{"gap_key": key, "cluster_key": key} for key in keys]},
    )


def _memory(key: str, *, binding: bool = False, citation: str = "") -> ConsolidatedMemory:
    return ConsolidatedMemory.objects.create(
        cluster_key=key,
        rule=f"Never skip the gate ({key}).",
        source_files=[f"feedback_{key}.md"],
        is_binding=binding,
        member_count=1,
        max_member_weight=90,
        verified_citation=citation,
        status=ConsolidatedMemory.Status.VERIFIED if citation else ConsolidatedMemory.Status.CANDIDATE,
    )


class TestPendingLedger(TestCase):
    def test_queueing_twice_keeps_one_entry_per_gap(self) -> None:
        umbrella = Ticket.objects.create(overlay="test", issue_url="https://github.com/o/r/issues/249")

        queue_pending(umbrella, [{"gap_key": "a", "title": "A"}, {"gap_key": "b", "title": "B"}])
        queue_pending(umbrella, [{"gap_key": "a", "title": "A renamed"}])

        umbrella.refresh_from_db()
        assert [entry["gap_key"] for entry in pending_entries(umbrella)] == ["a", "b"]

    def test_taking_removes_only_the_named_gaps(self) -> None:
        umbrella = Ticket.objects.create(overlay="test", issue_url="https://github.com/o/r/issues/249")
        queue_pending(umbrella, [{"gap_key": "a"}, {"gap_key": "b"}, {"gap_key": "c"}])

        taken = take_pending(umbrella, {"a", "c", "not-queued"})

        umbrella.refresh_from_db()
        assert [entry["gap_key"] for entry in taken] == ["a", "c"]
        assert [entry["gap_key"] for entry in pending_entries(umbrella)] == ["b"]


class TestGapDisposition(TestCase):
    def test_an_address_verifies_the_candidate_row_and_joins_the_delivered_subset(self) -> None:
        host = _host("a", "b")
        _memory("a")

        assert record_gap_disposition(host, "a", citation="task 5561 failed plan_missing") == ADDRESS

        host.refresh_from_db()
        row = ConsolidatedMemory.objects.get(cluster_key="a")
        assert (row.status, row.verified_citation) == (
            ConsolidatedMemory.Status.VERIFIED,
            "task 5561 failed plan_missing",
        )
        assert host.extra["dream_gap_claimed_delivered"] == ["a"]
        assert undispositioned_gap_keys(host) == ["b"]

    def test_a_reject_records_its_reason_and_touches_no_memory(self) -> None:
        host = _host("a")
        _memory("a")

        assert record_gap_disposition(host, "a", rejection="superseded by folding into hosts") == REJECT

        host.refresh_from_db()
        assert host.extra["dream_gap_dispositions"]["a"] == {
            "disposition": REJECT,
            "evidence": "superseded by folding into hosts",
        }
        assert "dream_gap_claimed_delivered" not in host.extra
        assert ConsolidatedMemory.objects.get(cluster_key="a").status == ConsolidatedMemory.Status.CANDIDATE
        assert undispositioned_gap_keys(host) == []

    def test_a_binding_row_is_never_retired_by_either_disposition(self) -> None:
        host = _host("a", "b")
        _memory("a", binding=True)
        _memory("b", binding=True, citation="already cited")

        record_gap_disposition(host, "a", citation="cited now")
        record_gap_disposition(host, "b", rejection="not a core gap")

        for key in ("a", "b"):
            row = ConsolidatedMemory.objects.get(cluster_key=key)
            assert row.disposition != ConsolidatedMemory.Disposition.RESOLVED_RETIRED
            assert row.status not in ConsolidatedMemory.Status.terminal()

    def test_exactly_one_of_citation_or_rejection_is_required(self) -> None:
        host = _host("a")
        for citation, rejection in (("", ""), ("cited", "rejected"), ("   ", "")):
            with (
                self.subTest(citation=citation, rejection=rejection),
                pytest.raises(DreamGapLedgerError, match="exactly one"),
            ):
                record_gap_disposition(host, "a", citation=citation, rejection=rejection)

    def test_a_gap_the_host_does_not_carry_is_refused(self) -> None:
        with pytest.raises(DreamGapLedgerError, match="not folded"):
            record_gap_disposition(_host("a"), "zzz", citation="cited")
