"""Tests for :class:`ScannedBroadcast` — the review-broadcast ledger and its emission gate.

The ledger key ``(channel, slack_ts)`` makes re-scanning safe. What it must
NOT do is double as the emission gate: a broadcast whose reviewer task was
never created, was deleted, or FAILED is a review that never reached anyone,
and it has to be re-emitted. ``awaiting_reviewer_dispatch`` is that gate.
"""

from django.test import TestCase

from teatree.core.models import BroadcastObservation, ScannedBroadcast, Session, Task, Ticket

CHANNEL = "C0DEMOCHAN1"
TS = "1779201478.501469"
MR_OPEN = "https://gitlab.example.com/team/project/-/merge_requests/7432"


def _observe(classification: str = ScannedBroadcast.Classification.PENDING) -> BroadcastObservation:
    return BroadcastObservation(
        channel=CHANNEL,
        slack_ts=TS,
        mr_urls=[MR_OPEN],
        classification=classification,
        overlay="teatree",
    )


def _reviewer_task(status: Task.Status = Task.Status.PENDING) -> Task:
    ticket = Ticket.objects.create(issue_url=MR_OPEN, role=Ticket.Role.REVIEWER, overlay="teatree")
    session = Session.objects.create(ticket=ticket, agent_id="t3:reviewer")
    return Task.objects.create(ticket=ticket, session=session, phase="reviewing", status=status)


class TestEmissionIsNotGatedOnLedgerNovelty(TestCase):
    def test_seen_row_without_reviewer_task_re_emits(self) -> None:
        first = ScannedBroadcast.record(_observe())
        assert first is not None

        assert ScannedBroadcast.record(_observe()) is not None

    def test_seen_row_with_live_reviewer_task_does_not_re_emit(self) -> None:
        row = ScannedBroadcast.record(_observe())
        assert row is not None
        row.attach_reviewer_task(str(_reviewer_task().pk))

        assert ScannedBroadcast.record(_observe()) is None

    def test_seen_row_with_completed_reviewer_task_does_not_re_emit(self) -> None:
        """A finished review is covered — re-emitting would re-review every tick.

        The broadcast signal carries no head SHA, so the downstream
        ``_already_reviewed_at_head`` dedup fails open and cannot suppress it.
        """
        row = ScannedBroadcast.record(_observe())
        assert row is not None
        row.attach_reviewer_task(str(_reviewer_task(Task.Status.COMPLETED).pk))

        assert ScannedBroadcast.record(_observe()) is None

    def test_seen_row_with_failed_reviewer_task_re_emits(self) -> None:
        row = ScannedBroadcast.record(_observe())
        assert row is not None
        row.attach_reviewer_task(str(_reviewer_task(Task.Status.FAILED).pk))

        assert ScannedBroadcast.record(_observe()) is not None

    def test_seen_row_whose_reviewer_task_was_deleted_re_emits(self) -> None:
        row = ScannedBroadcast.record(_observe())
        assert row is not None
        task = _reviewer_task()
        row.attach_reviewer_task(str(task.pk))
        task.delete()

        assert ScannedBroadcast.record(_observe()) is not None

    def test_all_merged_row_never_re_emits(self) -> None:
        merged = _observe(ScannedBroadcast.Classification.ALL_MERGED)
        assert ScannedBroadcast.record(merged) is not None

        assert ScannedBroadcast.record(merged) is None

    def test_sticky_manual_classification_still_suppresses(self) -> None:
        row = ScannedBroadcast.record(_observe())
        assert row is not None
        row.mark_manually_classified(ScannedBroadcast.Classification.PENDING)

        assert ScannedBroadcast.record(_observe()) is None

    def test_classification_change_still_returns_the_row(self) -> None:
        assert ScannedBroadcast.record(_observe()) is not None

        upgraded = ScannedBroadcast.record(_observe(ScannedBroadcast.Classification.ALL_MERGED))

        assert upgraded is not None
        assert upgraded.classification == ScannedBroadcast.Classification.ALL_MERGED
        assert upgraded.reclassified_at is not None


class TestAwaitingReviewerDispatch(TestCase):
    def test_pending_row_with_no_task_id_is_awaiting(self) -> None:
        row = ScannedBroadcast.record(_observe())
        assert row is not None

        assert row.awaiting_reviewer_dispatch is True

    def test_pending_row_with_non_numeric_task_id_is_awaiting(self) -> None:
        row = ScannedBroadcast.record(_observe())
        assert row is not None
        row.attach_reviewer_task("not-a-pk")

        assert row.awaiting_reviewer_dispatch is True
