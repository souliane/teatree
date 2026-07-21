"""Invariants for the owner-answer egress (slack-comms design, Phase 1/4).

- I4: an owner reply threads on the OWNER's own message ts (the authoritative
    ticket ``slack_answer.slack_ts``), never a stale DM-thread or a new root.
- I1: the owner answer is posted regardless of availability mode — patching
    ``resolve_mode`` to ``autonomous_away`` must not suppress the post (the path
    must never consult availability).
"""

from unittest.mock import MagicMock, patch

from django.test import TestCase

from teatree.agents.attempt_recorder import record_result_envelope
from teatree.core import availability
from teatree.core.mode_resolution import set_mode_override
from teatree.core.models import (
    DeferredQuestion,
    LoopPreset,
    LoopPresetOverride,
    PendingChatInjection,
    Session,
    Task,
    Ticket,
)


class TestOwnerAnswerThreading(TestCase):
    def _owner_dm_task(self, *, channel: str, slack_ts: str) -> Task:
        ticket = Ticket.objects.create(
            role=Ticket.Role.AUTHOR,
            state=Ticket.State.STARTED,
            overlay="acme",
            extra={"slack_answer": {"channel": channel, "slack_ts": slack_ts, "question": "hi"}},
        )
        session = Session.objects.create(ticket=ticket, agent_id="answering")
        task = Task.objects.create(ticket=ticket, session=session, phase="answering")
        task.claim(claimed_by="loop-slot")
        return task

    def test_reply_threads_on_owner_message_ts(self) -> None:
        channel, owner_ts = "D0OWNER", "1784474278.074869"
        task = self._owner_dm_task(channel=channel, slack_ts=owner_ts)
        backend = MagicMock()
        backend.post_reply.return_value = {"ok": True, "ts": "1784475031.6"}
        # A misleading agent-returned thread_ref must be ignored in favor of the
        # authoritative owner message ts carried on the ticket.
        with patch("teatree.core.backend_factory.messaging_from_overlay", return_value=backend):
            record_result_envelope(
                task,
                {"summary": "drafted", "answer": {"text": "here you go", "thread_ref": "WRONG/9.9"}},
            )
        _, kwargs = backend.post_reply.call_args
        assert kwargs["ts"] == owner_ts
        assert kwargs["channel"] == channel

    def test_answer_posts_under_autonomous_away(self) -> None:
        channel, owner_ts = "D0OWNER", "1700000000.000100"
        task = self._owner_dm_task(channel=channel, slack_ts=owner_ts)
        PendingChatInjection.objects.create(overlay="acme", channel=channel, slack_ts=owner_ts, text="hi")
        backend = MagicMock()
        backend.post_reply.return_value = {"ok": True, "ts": "1700000000.000200"}
        away = availability.Resolution(mode=availability.MODE_AUTONOMOUS_AWAY, source="override")
        with (
            patch("teatree.core.backend_factory.messaging_from_overlay", return_value=backend),
            patch.object(availability, "resolve_mode", return_value=away),
        ):
            record_result_envelope(
                task,
                {"summary": "drafted", "answer": {"text": "still answered", "thread_ref": ""}},
            )
        backend.post_reply.assert_called_once()
        assert DeferredQuestion.objects.count() == 0
        assert PendingChatInjection.objects.get().answered_at is not None

    def test_answer_posts_under_the_merged_offline_mode(self) -> None:
        # #61 invariant: the merge must not re-route owner replies through the
        # merged mode's defer path. Under a REAL offline override (the merged
        # holiday-away mode: defers_questions=True), the owner reply still sends
        # immediately and is NEVER parked as a DeferredQuestion.
        channel, owner_ts = "D0OWNER", "1700000000.000300"
        task = self._owner_dm_task(channel=channel, slack_ts=owner_ts)
        PendingChatInjection.objects.create(overlay="acme", channel=channel, slack_ts=owner_ts, text="hi")
        LoopPreset.objects.update_or_create(
            name="offline", defaults={"entries": {}, "defers_questions": True, "pauses_self_pump": True}
        )
        set_mode_override("offline")
        assert LoopPresetOverride.objects.current().preset_name == "offline"
        backend = MagicMock()
        backend.post_reply.return_value = {"ok": True, "ts": "1700000000.000400"}
        with patch("teatree.core.backend_factory.messaging_from_overlay", return_value=backend):
            record_result_envelope(task, {"summary": "drafted", "answer": {"text": "answered while offline"}})
        backend.post_reply.assert_called_once()
        assert DeferredQuestion.objects.count() == 0
        assert PendingChatInjection.objects.get().answered_at is not None
