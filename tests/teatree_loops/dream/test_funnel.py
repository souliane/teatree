from datetime import UTC, datetime, timedelta

from django.test import TestCase
from django.utils import timezone

from teatree.core.models import ConsolidatedMemory, DeferredQuestion, Ticket
from teatree.core.models.transition import TicketTransition
from teatree.loops.dream.batch_promote import PromotionBatch
from teatree.loops.dream.funnel import daily_funnel, question_class, question_class_status, record_funnel


class TestDreamFunnel(TestCase):
    def setUp(self) -> None:
        self.umbrella = Ticket.objects.create(issue_url="https://github.com/o/r/issues/249")

    def stranded_host(self, name: str, *, idle_days: int) -> Ticket:
        host = Ticket.objects.create(
            issue_url=f"https://github.com/o/r/issues/{name}", extra={"dream_gap_batch": [{"gap_key": name}]}
        )
        TicketTransition.objects.create(ticket=host, from_state="not_started", to_state="work_started")
        TicketTransition.objects.filter(ticket=host).update(created_at=timezone.now() - timedelta(days=idle_days))
        return host

    def test_snapshot_counts_the_ledger_and_the_hosts_still_stranded(self) -> None:
        ConsolidatedMemory.objects.create(
            cluster_key="g1",
            rule="Fix the path.",
            source_files=["feedback.md"],
            member_count=1,
            max_member_weight=90,
            disposition=ConsolidatedMemory.Disposition.CORE_GAP_NEEDS_TICKET,
        )
        rejected = self.stranded_host("56", idle_days=20)
        rejected.merge_extra(
            merge_into_dicts={"dream_gap_dispositions": {"56": {"disposition": "reject", "evidence": "misfit"}}}
        )
        self.stranded_host("57", idle_days=20)
        self.stranded_host("58", idle_days=2)
        self.umbrella.merge_extra(set_keys={"dream_gap_pending": [{"gap_key": "pending"}]})
        Ticket.objects.create(
            issue_url="https://github.com/o/r/issues/ignored",
            state=Ticket.State.IGNORED,
            extra={"dream_gap_batch": [{"gap_key": "ignored"}]},
        )
        Ticket.objects.create(
            issue_url="https://github.com/o/r/issues/orphan",
            state=Ticket.State.MERGED,
            extra={"dream_gap_batch": [{"gap_key": "orphan"}], "dream_gap_reconciled_at": "2026-10-01"},
        )

        entry = record_funnel(umbrella_url=self.umbrella.issue_url, batch=PromotionBatch(withheld=2))

        assert (entry["rows"], entry["core_gaps"], entry["withheld"], entry["folded"]) == (1, 1, 2, 4)
        assert (entry["pending"], entry["rejected"], entry["orphans"]) == (1, 1, 2)
        assert entry["stranded_hosts"] == 2

    def test_two_passes_in_one_day_keep_two_entries_and_the_daily_view_shows_the_last(self) -> None:
        morning = datetime(2026, 10, 25, 3, tzinfo=UTC)
        night = datetime(2026, 10, 25, 22, tzinfo=UTC)
        batch = PromotionBatch()

        record_funnel(umbrella_url=self.umbrella.issue_url, batch=PromotionBatch(withheld=5), now=morning)
        record_funnel(umbrella_url=self.umbrella.issue_url, batch=batch, now=night)

        self.umbrella.refresh_from_db()
        assert [entry["at"] for entry in self.umbrella.extra["dream_funnel"]] == [
            morning.isoformat(),
            night.isoformat(),
        ]
        assert [entry["withheld"] for entry in daily_funnel(self.umbrella.issue_url)] == [0]

    def test_a_retried_pass_does_not_duplicate_its_entry(self) -> None:
        now = datetime(2026, 10, 25, 9, tzinfo=UTC)

        record_funnel(umbrella_url=self.umbrella.issue_url, batch=PromotionBatch(), now=now)
        record_funnel(umbrella_url=self.umbrella.issue_url, batch=PromotionBatch(), now=now)

        self.umbrella.refresh_from_db()
        assert len(self.umbrella.extra["dream_funnel"]) == 1

    def test_history_older_than_the_retention_window_is_trimmed(self) -> None:
        now = datetime(2026, 10, 25, 9, tzinfo=UTC)
        record_funnel(umbrella_url=self.umbrella.issue_url, batch=PromotionBatch(), now=now - timedelta(days=121))
        record_funnel(umbrella_url=self.umbrella.issue_url, batch=PromotionBatch(), now=now - timedelta(days=119))

        record_funnel(umbrella_url=self.umbrella.issue_url, batch=PromotionBatch(), now=now)

        self.umbrella.refresh_from_db()
        assert [entry["at"] for entry in self.umbrella.extra["dream_funnel"]] == [
            (now - timedelta(days=119)).isoformat(),
            now.isoformat(),
        ]


class TestQuestionClass(TestCase):
    def test_owner_action_markers_share_the_action_class(self) -> None:
        assert question_class("owner-action:attest:one") == "owner-action:attest"
        assert question_class("owner-action:attest:two") == "owner-action:attest"
        assert question_class("repair-loop:host:one") == "repair-loop"

    def test_answering_a_recent_card_does_not_mark_its_generator_fixed(self) -> None:
        now = datetime(2026, 10, 25, 9, tzinfo=UTC)
        DeferredQuestion.objects.create(
            question="May I continue?",
            dedupe_marker="owner-action:attest:one",
            created_at=now - timedelta(days=1),
            answered_at=now - timedelta(hours=12),
        )

        status = question_class_status("owner-action:attest", now=now)

        assert status is not None
        assert status.fixed is False
        assert sum(status.daily_counts) == 1

    def test_a_card_seven_days_ago_keeps_the_class_open(self) -> None:
        now = datetime(2026, 10, 25, 9, tzinfo=UTC)
        DeferredQuestion.objects.create(
            question="May I continue?", dedupe_marker="repair-loop:one", created_at=now - timedelta(days=7)
        )

        status = question_class_status("repair-loop", now=now)

        assert status is not None
        assert status.fixed is False
        assert status.daily_counts == (1, 0, 0, 0, 0, 0, 0)

    def test_seven_complete_zero_card_days_mark_a_known_class_fixed(self) -> None:
        now = datetime(2026, 10, 25, 9, tzinfo=UTC)
        DeferredQuestion.objects.create(
            question="May I continue?",
            dedupe_marker="repair-loop:one",
            created_at=now - timedelta(days=8),
        )

        status = question_class_status("repair-loop", now=now)

        assert status is not None
        assert status.fixed is True
        assert status.daily_counts == (0, 0, 0, 0, 0, 0, 0)
        assert question_class_status("unseen", now=now) is None
