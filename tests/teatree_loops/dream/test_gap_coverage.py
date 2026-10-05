from django.test import TestCase

from teatree.core.models import ConsolidatedMemory, Ticket
from teatree.loops.dream.gap_coverage import gap_coverage

UMBRELLA = "https://gitlab.com/o/factory/-/work_items/249"


def _host(number: int, *keys: str, state: str = Ticket.State.WORK_STARTED, **extra: object) -> Ticket:
    return Ticket.objects.create(
        issue_url=f"https://gitlab.com/o/factory/-/work_items/{number}",
        state=state,
        extra={"dream_gap_batch": [{"gap_key": key, "cluster_key": key} for key in keys], **extra},
    )


def _ticketed_row(key: str, url: str) -> ConsolidatedMemory:
    row = ConsolidatedMemory.objects.create(
        cluster_key=key, rule=f"rule {key}", source_files=[], member_count=1, max_member_weight=90
    )
    row.classify_core_gap()
    row.mark_ticketed(url)
    return row


class TestStrandedMemoryRows(TestCase):
    def test_a_ticketed_row_no_owner_holds_is_stranded(self) -> None:
        _ticketed_row("lost", f"{UMBRELLA}#dream-batch=abc")

        report = gap_coverage(umbrella_url=UMBRELLA)

        assert (report.ok, report.stranded) == (False, ["lost"])

    def test_a_ticketed_row_a_live_host_holds_is_not_stranded(self) -> None:
        _host(56, "held")
        _ticketed_row("held", f"{UMBRELLA}#dream-batch=abc")

        assert gap_coverage(umbrella_url=UMBRELLA).stranded == []

    def test_a_rejected_gap_on_a_reconciled_host_is_still_owned(self) -> None:
        _host(
            56,
            "rejected",
            state=Ticket.State.MERGED,
            dream_gap_reconciled_at="2026-09-26T00:00:00",
            dream_gap_dispositions={"rejected": {"disposition": "reject", "evidence": "not a core gap"}},
        )
        _ticketed_row("rejected", f"{UMBRELLA}#dream-batch=abc")

        report = gap_coverage(umbrella_url=UMBRELLA)

        assert (report.ok, report.stranded) == (True, [])

    def test_a_row_ticketed_on_a_real_pr_is_not_a_dream_gap(self) -> None:
        _ticketed_row("elsewhere", "https://gitlab.com/o/factory/-/merge_requests/7")

        assert gap_coverage(umbrella_url=UMBRELLA).stranded == []


class TestAGapAReconciledHostDropped(TestCase):
    RECONCILED = "2026-09-26T00:00:00"

    def test_a_dropped_gap_held_back_in_the_drain_is_an_orphan(self) -> None:
        _host(56, "requeued", state=Ticket.State.MERGED, dream_gap_reconciled_at=self.RECONCILED)
        _ticketed_row("requeued", f"{UMBRELLA}#dream-batch=abc").reopen_core_gap()

        report = gap_coverage(umbrella_url=UMBRELLA)

        assert (report.ok, report.orphan) == (False, ["requeued"])


class TestHostScope(TestCase):
    def test_a_host_scoped_proof_ignores_another_hosts_orphan(self) -> None:
        host = _host(56, "mine")
        _ticketed_row("someone-elses", f"{UMBRELLA}#dream-batch=abc")
        _host(897, "retired", state=Ticket.State.IGNORED)
        host.merge_extra(
            merge_into_dicts={"dream_gap_dispositions": {"mine": {"disposition": "reject", "evidence": "x"}}}
        )

        report = gap_coverage(umbrella_url=UMBRELLA, host=host)

        assert report.ok
        assert report.orphan == []
        assert (report.stranded, report.retired_owner) == ([], [])

    def test_a_host_scoped_proof_includes_its_own_dropped_gap(self) -> None:
        host = _host(56, "kept", "dropped", state=Ticket.State.MERGED, dream_gap_reconciled_at="2026-09-28")
        host.merge_extra(
            merge_into_dicts={"dream_gap_dispositions": {"kept": {"disposition": "reject", "evidence": "x"}}}
        )
        _host(57, "elsewhere", state=Ticket.State.MERGED, dream_gap_reconciled_at="2026-09-28")

        scoped = gap_coverage(umbrella_url=UMBRELLA, host=host)
        global_report = gap_coverage(umbrella_url=UMBRELLA)

        assert (scoped.ok, scoped.orphan) == (False, ["dropped"])
        assert global_report.orphan == ["dropped", "elsewhere"]

    def test_a_host_scoped_proof_still_fails_on_its_own_duplicate(self) -> None:
        host = _host(56, "shared")
        _host(110, "shared")
        host.merge_extra(
            merge_into_dicts={"dream_gap_dispositions": {"shared": {"disposition": "reject", "evidence": "x"}}}
        )

        report = gap_coverage(umbrella_url=UMBRELLA, host=host)

        assert not report.ok
        assert list(report.duplicate) == ["shared"]

    def test_a_host_scoped_proof_fails_on_an_undispositioned_gap(self) -> None:
        host = _host(56, "open")

        assert gap_coverage(umbrella_url=UMBRELLA, host=host).undispositioned == ["open"]
