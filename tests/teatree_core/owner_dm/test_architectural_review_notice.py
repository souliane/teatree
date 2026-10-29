"""Owner notification keys for the periodic architectural review."""

from functools import partial
from unittest.mock import MagicMock, patch

from django.test import TestCase

from teatree.core.models import BotPing, Session, Task, Ticket
from teatree.core.owner_dm.architectural_review_notice import announce_completed, announce_failed, announce_held


class TestArchitecturalReviewNotice(TestCase):
    def test_each_outcome_posts_once_to_owner(self) -> None:
        ticket = Ticket.objects.create(issue_url="architectural-review://test", overlay="test")
        task = Task.objects.create(
            ticket=ticket,
            session=Session.objects.create(ticket=ticket),
            phase="architectural_review",
            status=Task.Status.COMPLETED,
        )
        backend = MagicMock()
        backend.open_dm.return_value = "D-OWNER"
        backend.post_message.return_value = {"ok": True, "ts": "1700000000.000001"}
        backend.get_permalink.return_value = "https://example.com/owner-message"
        with (
            patch("teatree.core.notify.resolve_owner_dm_backend", return_value=(backend, None)),
            patch("teatree.core.notify.resolve_user_id", return_value="U_OWNER"),
        ):
            for announce, detail in (
                (announce_completed, "Opened https://github.com/example/repo/pull/1"),
                (announce_failed, "credential exhausted"),
                (partial(announce_held, hold_kind="admission"), "machine load HALT"),
            ):
                announce(task, detail)
                announce(task, detail)

        assert backend.post_message.call_count == 3
        assert set(BotPing.objects.values_list("status", flat=True)) == {BotPing.Status.SENT}

    def test_each_outcome_has_one_durable_row_per_task(self) -> None:
        ticket = Ticket.objects.create(issue_url="architectural-review://test", overlay="test")
        task = Task.objects.create(
            ticket=ticket,
            session=Session.objects.create(ticket=ticket),
            phase="architectural_review",
            status=Task.Status.FAILED,
        )
        announce_held(task, "machine load HALT", hold_kind="admission")
        announce_held(task, "machine load HALT", hold_kind="admission")
        announce_failed(task, "No credential configured")
        announce_failed(task, "No credential configured")

        keys = list(BotPing.objects.order_by("idempotency_key").values_list("idempotency_key", flat=True))
        assert keys == [f"architectural-review:failed:{task.pk}", f"architectural-review:held:{task.pk}:admission"]

    def test_failed_notice_is_capped_and_keeps_its_opening(self) -> None:
        ticket = Ticket.objects.create(issue_url="architectural-review://test", overlay="test")
        task = Task.objects.create(
            ticket=ticket,
            session=Session.objects.create(ticket=ticket),
            phase="architectural_review",
            status=Task.Status.FAILED,
        )

        announce_failed(task, "Unclassified: " + "word " * 600)

        notice = BotPing.objects.get(idempotency_key=f"architectural-review:failed:{task.pk}").text
        assert len(notice) <= 400
        assert notice.startswith("Architectural review for test failed: Unclassified: word")
