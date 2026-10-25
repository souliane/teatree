from datetime import UTC, datetime, timedelta

from django.test import TestCase

from teatree.core.models import ConsolidatedMemory, DeferredQuestion, Ticket
from teatree.loops.dream.batch_promote import PromotionBatch
from teatree.loops.dream.funnel import question_class, question_class_status, record_funnel
from teatree.loops.dream.stranded_hosts import StrandedReleaseReport


class TestDreamFunnel(TestCase):
    def test_daily_snapshot_counts_the_ledger_and_trims_old_samples(self) -> None:
        now = datetime(2026, 10, 25, 9, tzinfo=UTC)
        umbrella = Ticket.objects.create(
            issue_url="https://github.com/o/r/issues/249",
            extra={"dream_funnel": [{"at": (now - timedelta(days=121)).isoformat(), "rows": 99}]},
        )
        ConsolidatedMemory.objects.create(
            cluster_key="g1",
            rule="Fix the path.",
            source_files=["feedback.md"],
            member_count=1,
            max_member_weight=90,
            disposition=ConsolidatedMemory.Disposition.CORE_GAP_NEEDS_TICKET,
        )
        batch = PromotionBatch(withheld=2)

        first = record_funnel(
            umbrella_url=umbrella.issue_url, batch=batch, release=StrandedReleaseReport(1, 2), now=now
        )
        second = record_funnel(
            umbrella_url=umbrella.issue_url, batch=batch, release=StrandedReleaseReport(1, 2), now=now
        )

        umbrella.refresh_from_db()
        assert first == second
        assert len(umbrella.extra["dream_funnel"]) == 1
        assert (first["rows"], first["core_gaps"], first["withheld"], first["stranded_hosts"]) == (1, 1, 2, 1)


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
