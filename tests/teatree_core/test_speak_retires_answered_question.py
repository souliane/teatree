"""Out-of-band threaded DM reply retires the answered question (#2053).

A user-question Slack DM lands as a :class:`PendingChatInjection` row. The
agent answers it out-of-band via ``t3 <overlay> notify post --thread-ts``,
which routes through the single bot->user DM chokepoint
:func:`teatree.core.speak.deliver_user_dm`. Before #2053 that path wrote no
answer-stamp, so the row stayed ``loop_replied_at IS NULL`` forever and the
reactive Slack-answer cycle re-delegated an answerer Task after every
cancel. The chokepoint now stamps the matching row on a threaded reply, so
the question is retired from BOTH the cycle work-queue (``loop_replied_at``)
and the Stop-hook gate (``answered_at``).

The Slack network is faked at the backend boundary; the chokepoint, the
model CAS, and the reactive cycle all run for real.
"""

from unittest.mock import patch

from django.test import TestCase

from teatree.core import speak as speak_mod
from teatree.core.models import PendingChatInjection, Task
from teatree.loop.slack_answer.cycle import run_slack_answer_cycle
from teatree.types import RawAPIDict, SpeakConfig


class _Backend:
    def post_message(self, *, channel: str, text: str, thread_ts: str = "") -> RawAPIDict:
        _ = (channel, text, thread_ts)
        return {"ok": True, "ts": "9999.0001"}

    def post_reply(self, *, channel: str, ts: str, text: str) -> RawAPIDict:
        _ = (channel, ts, text)
        return {"ok": True}

    def react(self, *, channel: str, ts: str, emoji: str) -> RawAPIDict:
        _ = (channel, ts, emoji)
        return {"ok": True}

    def get_permalink(self, *, channel: str, ts: str) -> str:
        _ = (channel, ts)
        return "https://slack/p1"


def _deliver(backend: _Backend, *, thread_ts: str) -> None:
    with patch.object(speak_mod, "_resolve_speak_safe", return_value=SpeakConfig()):
        speak_mod.deliver_user_dm(backend, channel="D_ME", text="migration ran out of order", thread_ts=thread_ts)


class TestThreadedReplyRetiresQuestion(TestCase):
    def test_threaded_reply_marks_matching_row_loop_replied(self) -> None:
        PendingChatInjection.record(channel="D_ME", slack_ts="1780757338.674389", text="why was it cancelled?")

        _deliver(_Backend(), thread_ts="1780757338.674389")

        row = PendingChatInjection.objects.get()
        assert row.loop_replied_at is not None
        assert row.answered_at is not None
        assert row.answer_kind == PendingChatInjection.AnswerKind.QUESTION_REPLY

    def test_retired_row_drops_out_of_loop_unreplied(self) -> None:
        PendingChatInjection.record(channel="D_ME", slack_ts="1780757338.674389", text="why?")

        _deliver(_Backend(), thread_ts="1780757338.674389")

        assert list(PendingChatInjection.loop_unreplied()) == []

    def test_cycle_files_no_answerer_task_after_out_of_band_reply(self) -> None:
        PendingChatInjection.record(channel="D_ME", slack_ts="1780757338.674389", text="why was it cancelled?")

        _deliver(_Backend(), thread_ts="1780757338.674389")
        run_slack_answer_cycle(messaging_resolver=lambda _overlay: _Backend())

        assert Task.objects.filter(phase="answering").count() == 0

    def test_cycle_files_one_task_when_no_reply_present(self) -> None:
        PendingChatInjection.record(channel="D_ME", slack_ts="1780757338.674389", text="why was it cancelled?")

        run_slack_answer_cycle(messaging_resolver=lambda _overlay: _Backend())

        assert Task.objects.filter(phase="answering").count() == 1

    def test_unrelated_thread_reply_does_not_retire_other_rows(self) -> None:
        PendingChatInjection.record(channel="D_ME", slack_ts="1780757338.674389", text="why?")

        _deliver(_Backend(), thread_ts="2222222222.000000")

        row = PendingChatInjection.objects.get()
        assert row.loop_replied_at is None
        assert row.answered_at is None

    def test_top_level_dm_without_thread_does_not_retire(self) -> None:
        PendingChatInjection.record(channel="D_ME", slack_ts="1780757338.674389", text="why?")

        _deliver(_Backend(), thread_ts="")

        row = PendingChatInjection.objects.get()
        assert row.loop_replied_at is None
