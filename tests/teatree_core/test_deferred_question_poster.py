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
from teatree.core.models import BotPing, DeferredQuestion, IncomingEvent
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

    def test_injected_backend_delivers_without_overlay_resolution(self) -> None:
        # F2: the global-tick poster is handed an explicit backend so it delivers
        # even with no ``T3_OVERLAY_NAME`` — no overlay resolution needed.
        question = DeferredQuestion.record("Which DB host?", session_id="s")
        backend = _backend()

        with patch.object(notify_module, "messaging_from_overlay", return_value=None):
            delivered, total = drain_unmirrored_deferred_questions(user_id="U_ME", backend=backend)

        assert (delivered, total) == (1, 1)
        backend.post_message.assert_called_once()
        question.refresh_from_db()
        assert question.slack_ts == "1700000000.000000"

    def test_internal_audience_row_is_not_dmed(self) -> None:
        # Phase 2: an INTERNAL escalation (repair-loop / dispatch health) is
        # excluded from the mirror poster, so it never DMs the owner.
        DeferredQuestion.record(
            "Repair-loop stall on ticket 1",
            session_id="s",
            audience=DeferredQuestion.Audience.INTERNAL,
        )
        backend = _backend()
        with patch.object(notify_module, "messaging_from_overlay", return_value=backend):
            delivered, total = drain_unmirrored_deferred_questions(user_id="U_ME")
        assert (delivered, total) == (0, 0)
        backend.post_message.assert_not_called()

    def test_owner_question_row_is_still_dmed(self) -> None:
        # The owner-audience row alongside an internal one is the only one posted.
        owner = DeferredQuestion.record("Owner decision?", session_id="s")
        DeferredQuestion.record(
            "internal stall",
            session_id="s",
            audience=DeferredQuestion.Audience.INTERNAL,
        )
        backend = _backend()
        with patch.object(notify_module, "messaging_from_overlay", return_value=backend):
            delivered, total = drain_unmirrored_deferred_questions(user_id="U_ME")
        assert (delivered, total) == (1, 1)
        backend.post_message.assert_called_once()
        owner.refresh_from_db()
        assert owner.slack_ts == "1700000000.000000"

    def test_answered_row_is_not_posted(self) -> None:
        question = DeferredQuestion.record("Which DB host?", session_id="s")
        question.apply_answer("postgres-1", resolved_via="local")
        backend = _backend()

        with patch.object(notify_module, "messaging_from_overlay", return_value=backend):
            delivered, total = drain_unmirrored_deferred_questions(user_id="U_ME")

        assert (delivered, total) == (0, 0)
        backend.post_message.assert_not_called()


class TestMirrorIsPostedAtThreadRoot(TestCase):
    """The mirror never nests under the owner's active DM thread.

    The stamped ``slack_ts`` is the reply-binding identity: a Slack reply
    carries its thread ROOT's ts, so a mirror posted one level down inside the
    conversation the owner happened to be in records a ts no reply can name.
    """

    def test_posts_at_root_even_mid_conversation(self) -> None:
        IncomingEvent.objects.create(
            source=IncomingEvent.Source.SLACK,
            channel_ref="D-USER",
            thread_ref="1699999999.000000",
            idempotency_key="slack:Ev-owner-thread",
        )
        question = DeferredQuestion.record("Which DB host?", session_id="s")
        backend = _backend()

        with patch.object(notify_module, "messaging_from_overlay", return_value=backend):
            drain_unmirrored_deferred_questions(user_id="U_ME", backend=backend)

        assert backend.post_message.call_args.kwargs["thread_ts"] == ""
        question.refresh_from_db()
        assert question.slack_ts == "1700000000.000000"


class TestTargetedDrainByRef(TestCase):
    """``only_ref`` — the capture-time kick that must not queue behind the backlog.

    ``unmirrored_pending()`` is oldest-first and the tick batch is capped, so a
    freshly-recorded row sits at the TAIL: a plain drain leaves a headless ask
    waiting ``ceil(backlog/cap)`` ticks. The deny arm targets its own row instead.
    """

    def test_targets_a_row_sitting_past_the_per_tick_cap(self) -> None:
        for i in range(5):
            DeferredQuestion.record(f"older #{i}", session_id="s", tool_use_id=f"older-{i}")
        fresh = DeferredQuestion.record("the blocker", session_id="s", tool_use_id="fresh-1")
        backend = _backend()

        with patch.object(notify_module, "messaging_from_overlay", return_value=backend):
            delivered, total = drain_unmirrored_deferred_questions(user_id="U_ME", only_ref=fresh.stable_notify_ref)

        assert (delivered, total) == (1, 1)
        fresh.refresh_from_db()
        assert fresh.slack_ts == "1700000000.000000"
        assert backend.post_message.call_count == 1
        assert DeferredQuestion.objects.filter(slack_ts="").count() == 5

    def test_unknown_ref_delivers_nothing(self) -> None:
        DeferredQuestion.record("older", session_id="s", tool_use_id="older-1")
        backend = _backend()

        with patch.object(notify_module, "messaging_from_overlay", return_value=backend):
            delivered, total = drain_unmirrored_deferred_questions(user_id="U_ME", only_ref="no-such-ref")

        assert (delivered, total) == (0, 0)
        assert backend.post_message.call_count == 0

    def test_already_mirrored_ref_is_a_no_op(self) -> None:
        row = DeferredQuestion.record("the blocker", session_id="s", tool_use_id="fresh-1")
        backend = _backend()

        with patch.object(notify_module, "messaging_from_overlay", return_value=backend):
            drain_unmirrored_deferred_questions(user_id="U_ME", only_ref=row.stable_notify_ref)
            delivered, total = drain_unmirrored_deferred_questions(user_id="U_ME", only_ref=row.stable_notify_ref)

        assert (delivered, total) == (0, 0)
        assert backend.post_message.call_count == 1
