"""Review hold preserves work and sends one post-commit owner note."""

from unittest.mock import patch

from django.test import TestCase

from teatree.core.models import Ticket, Worktree
from teatree.core.models.shipping_hold import hold_unshippable_review


class TestShippingHold(TestCase):
    def _ticket(self) -> Ticket:
        return Ticket.objects.create(
            overlay="test",
            role=Ticket.Role.AUTHOR,
            state=Ticket.State.SELF_REVIEWED,
            issue_url="https://github.com/souliane/teatree/issues/5346",
            extra={"shipping_skipped": "no shippable diff"},
        )

    def test_rowless_note_sends_once_after_commit(self) -> None:
        ticket = self._ticket()
        with patch("teatree.core.review.shipping_hold_notice.notify_user", return_value=True) as notify:
            with self.captureOnCommitCallbacks(execute=True) as callbacks:
                hold_unshippable_review(ticket)
                notify.assert_not_called()
            assert len(callbacks) == 1
            notify.assert_called_once()
            assert notify.call_args.kwargs["idempotency_key"] == f"shipping-hold:{ticket.pk}"
            with self.captureOnCommitCallbacks(execute=True) as repeated:
                hold_unshippable_review(ticket)
            assert repeated == []
        ticket.refresh_from_db()
        assert ticket.extra["shipping_hold"]["reason"] == "no worktree row"
        assert ticket.state == Ticket.State.SELF_REVIEWED

    def test_row_with_missing_path_holds_without_note(self) -> None:
        ticket = self._ticket()
        Worktree.objects.create(ticket=ticket, overlay="test", repo_path="souliane/teatree", branch="5346-work")
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            hold_unshippable_review(ticket)
        assert callbacks == []
        ticket.refresh_from_db()
        assert ticket.extra["shipping_hold"]["reason"] == "awaiting the workspace and forge check"

    def test_failed_note_does_not_undo_hold(self) -> None:
        ticket = self._ticket()
        with (
            patch("teatree.core.review.shipping_hold_notice.notify_user", side_effect=RuntimeError("unavailable")),
            self.captureOnCommitCallbacks(execute=True),
        ):
            hold_unshippable_review(ticket)
        ticket.refresh_from_db()
        assert ticket.extra["shipping_hold"]["reason"] == "no worktree row"
