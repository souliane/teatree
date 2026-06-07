"""The shared bot→user DM chokepoint does NOT retire questions (#2053).

A user-question Slack DM lands as a :class:`PendingChatInjection` row.
:func:`teatree.core.speak.deliver_user_dm` is the ONE chokepoint EVERY
bot→user DM passes through — the ``notify post --thread-ts`` deliberate
answer AND every unrelated INFO/status DM that ``notify_user`` threads via
``_active_dm_thread``. PR #2103's first cut retired the question here, so an
innocent INFO DM threaded under a still-open question silently retired it.

The retire is now owned by the deliberate threaded-answer egress (the
self-DM branch of :meth:`OnBehalfSlackEgress.post`); the chokepoint reverts
to pure delivery. These tests pin that: a threaded DM through the bare
chokepoint leaves the matching question untouched.
"""

from unittest.mock import patch

from django.test import TestCase

from teatree.core import speak as speak_mod
from teatree.core.models import PendingChatInjection
from teatree.types import RawAPIDict, SpeakConfig

_QUESTION_TS = "1780757338.674389"


class _Backend:
    def post_message(self, *, channel: str, text: str, thread_ts: str = "") -> RawAPIDict:
        _ = (channel, text, thread_ts)
        return {"ok": True, "ts": "9999.0001"}


def _deliver(*, thread_ts: str) -> None:
    with patch.object(speak_mod, "_resolve_speak_safe", return_value=SpeakConfig()):
        speak_mod.deliver_user_dm(_Backend(), channel="D_ME", text="build is green", thread_ts=thread_ts)


class TestChokepointDoesNotRetire(TestCase):
    def test_info_dm_threaded_under_open_question_does_not_retire_it(self) -> None:
        PendingChatInjection.record(channel="D_ME", slack_ts=_QUESTION_TS, text="why was it cancelled?")

        _deliver(thread_ts=_QUESTION_TS)

        row = PendingChatInjection.objects.get()
        assert row.loop_replied_at is None
        assert row.answered_at is None
        assert row.answer_kind == ""

    def test_top_level_dm_leaves_question_untouched(self) -> None:
        PendingChatInjection.record(channel="D_ME", slack_ts=_QUESTION_TS, text="why?")

        _deliver(thread_ts="")

        row = PendingChatInjection.objects.get()
        assert row.loop_replied_at is None
        assert row.answered_at is None
