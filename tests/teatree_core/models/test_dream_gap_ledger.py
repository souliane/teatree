from unittest.mock import patch

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
    def test_misfit_returns_gap_to_pending_without_recording_a_rejection(self) -> None:
        umbrella_url = "https://github.com/o/r/issues/249"
        umbrella = Ticket.objects.create(overlay="test", issue_url=umbrella_url)
        host = _host("a")

        with patch("teatree.core.models.dream_gap_ledger.dream_umbrella_url", return_value=umbrella_url):
            result = record_gap_disposition(host, "a", misfit="wrong host")

        host.refresh_from_db()
        umbrella.refresh_from_db()
        assert result == "misfit"
        assert host.extra["dream_gap_batch"] == []
        assert host.extra.get("dream_gap_dispositions", {}) == {}
        assert pending_entries(umbrella)[0]["declined_hosts"] == [host.pk]

    def test_misfit_removes_the_old_offer_stamp_and_delivered_claim(self) -> None:
        umbrella_url = "https://github.com/o/r/issues/249"
        umbrella = Ticket.objects.create(overlay="test", issue_url=umbrella_url)
        queue_pending(umbrella, [{"gap_key": "never-offered", "cluster_key": "never-offered"}])
        host = _host("a")
        host.merge_extra(
            set_keys={
                "dream_gap_batch": [{"gap_key": "a", "cluster_key": "a", "offered_at": "2026-10-01T00:00:00+00:00"}],
                "dream_gap_claimed_delivered": ["a"],
            }
        )

        with patch("teatree.core.models.dream_gap_ledger.dream_umbrella_url", return_value=umbrella_url):
            record_gap_disposition(host, "a", misfit="wrong host")

        umbrella.refresh_from_db()
        host.refresh_from_db()
        assert "offered_at" not in pending_entries(umbrella)[0]
        assert [entry["gap_key"] for entry in pending_entries(umbrella)] == ["a", "never-offered"]
        assert host.extra["dream_gap_claimed_delivered"] == []

    def test_second_distinct_decline_requeues_before_retaining_on_third(self) -> None:
        umbrella_url = "https://github.com/o/r/issues/249"
        umbrella = Ticket.objects.create(overlay="test", issue_url=umbrella_url)
        row = _memory("a")
        host = _host("a")
        host.extra["dream_gap_batch"][0]["declined_hosts"] = [100]
        host.save(update_fields=["extra"])

        with patch("teatree.core.models.dream_gap_ledger.dream_umbrella_url", return_value=umbrella_url):
            record_gap_disposition(host, "a", misfit="wrong host")

        umbrella.refresh_from_db()
        row.refresh_from_db()
        assert pending_entries(umbrella)[0]["declined_hosts"] == [100, host.pk]
        assert row.disposition != ConsolidatedMemory.Disposition.USER_SPECIFIC_KEEP

    def test_a_repeated_decline_by_one_host_counts_once(self) -> None:
        umbrella_url = "https://github.com/o/r/issues/249"
        umbrella = Ticket.objects.create(overlay="test", issue_url=umbrella_url)
        host = _host("a")
        host.extra["dream_gap_batch"][0]["declined_hosts"] = [100, host.pk]
        host.save(update_fields=["extra"])

        with patch("teatree.core.models.dream_gap_ledger.dream_umbrella_url", return_value=umbrella_url):
            record_gap_disposition(host, "a", misfit="still wrong")

        umbrella.refresh_from_db()
        assert pending_entries(umbrella)[0]["declined_hosts"] == [100, host.pk]

    def test_misfit_refuses_an_addressed_or_rejected_gap(self) -> None:
        host = _host("a", "b")
        host.merge_extra(
            merge_into_dicts={
                "dream_gap_dispositions": {
                    "a": {"disposition": ADDRESS, "evidence": "done"},
                    "b": {"disposition": REJECT, "evidence": "done"},
                }
            }
        )
        for key in ("a", "b"):
            with self.subTest(key=key), pytest.raises(DreamGapLedgerError, match="already dispositioned"):
                record_gap_disposition(host, key, misfit="wrong host")

    def test_third_distinct_host_decline_keeps_the_memory_without_requeueing(self) -> None:
        umbrella_url = "https://github.com/o/r/issues/249"
        umbrella = Ticket.objects.create(overlay="test", issue_url=umbrella_url)
        row = _memory("a")
        host = _host("a")
        host.extra["dream_gap_batch"][0]["declined_hosts"] = [100, 101]
        host.save(update_fields=["extra"])

        with patch("teatree.core.models.dream_gap_ledger.dream_umbrella_url", return_value=umbrella_url):
            result = record_gap_disposition(host, "a", misfit="no matching host")

        host.refresh_from_db()
        row.refresh_from_db()
        umbrella.refresh_from_db()
        assert result == "misfit"
        assert host.extra["dream_gap_batch"] == []
        assert pending_entries(umbrella) == []
        assert row.disposition == ConsolidatedMemory.Disposition.USER_SPECIFIC_KEEP

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
