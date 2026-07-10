"""Tick-level poster: mirror un-mirrored DeferredQuestion rows to Slack.

The headless lane (and the orphaned ``task_repair._escalate_stall`` rows)
record a ``DeferredQuestion`` with no ``slack_ts`` — nobody posts it today.
``drain_unmirrored_deferred_questions`` posts each un-mirrored pending row
to the user's Slack DM and stamps the mirror coordinates so the reply
scanner can later bind a reply. It is idempotent (BotPing dedup + the
``slack_ts != ""`` filter) so re-running a tick never double-posts.
"""

from unittest.mock import MagicMock, patch

from django.test import TestCase

from teatree.core import notify as notify_module
from teatree.core.models import BotPing, DeferredQuestion
from teatree.core.notify_question_drains import drain_unmirrored_deferred_questions


def _backend(*, ts: str = "1700000000.000000") -> MagicMock:
    b = MagicMock()
    b.open_dm.return_value = "D-USER"
    b.post_message.return_value = {"ok": True, "ts": ts}
    b.get_permalink.return_value = "https://acme.slack.com/archives/D-USER/p1700000000000000"
    return b


class TestDrainUnmirroredDeferredQuestions(TestCase):
    def test_posts_unmirrored_and_stamps_mirror_coordinates(self) -> None:
        question = DeferredQuestion.record("Which DB host?", session_id="s")
        assert question.slack_ts == ""
        backend = _backend()

        with patch.object(notify_module, "messaging_from_overlay", return_value=backend):
            delivered, total = drain_unmirrored_deferred_questions(user_id="U_ME")

        assert (delivered, total) == (1, 1)
        question.refresh_from_db()
        assert question.slack_channel == "D-USER"
        assert question.slack_ts == "1700000000.000000"
        assert BotPing.objects.filter(
            idempotency_key=f"mirror-deferred-question:{question.stable_notify_ref}",
            status=BotPing.Status.SENT,
        ).exists()

    def test_idempotent_skips_already_mirrored_row(self) -> None:
        DeferredQuestion.record("Which DB host?", session_id="s")
        backend = _backend()

        with patch.object(notify_module, "messaging_from_overlay", return_value=backend):
            drain_unmirrored_deferred_questions(user_id="U_ME")
            _mirrored, total = drain_unmirrored_deferred_questions(user_id="U_ME")

        assert total == 0
        assert backend.post_message.call_count == 1

    def test_answered_row_is_not_posted(self) -> None:
        question = DeferredQuestion.record("Which DB host?", session_id="s")
        question.apply_answer("postgres-1", resolved_via="local")
        backend = _backend()

        with patch.object(notify_module, "messaging_from_overlay", return_value=backend):
            delivered, total = drain_unmirrored_deferred_questions(user_id="U_ME")

        assert (delivered, total) == (0, 0)
        backend.post_message.assert_not_called()
