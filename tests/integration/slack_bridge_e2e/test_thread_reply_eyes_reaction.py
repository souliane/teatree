"""Thread-reply :eyes: receipt end-to-end (#1366).

The inbound-to-reactive chain for a user message that arrives as a Slack
*thread reply* (not a top-level DM): the bot posts a top-level message,
the user replies in-thread, and the reactive Slack-answer loop must stamp
the same :eyes: receipt it stamps on a top-level DM. The whole chain runs
unmodified — real ``SlackBotBackend.fetch_dms`` (with the
``conversations.replies`` fan-out), real ``SlackDmInboundScanner``, real
``PendingChatInjection`` rows, real ``run_slack_answer_cycle`` — with only
the ``httpx`` network faked. The load-bearing assertion is that
``reactions.add`` is called on the *thread reply's* ``ts``, not the thread
root's.
"""

import pytest

from teatree.backends.slack.bot import SlackBotBackend
from teatree.core.models import PendingChatInjection
from teatree.loop.scanners.slack_dm_inbound import SlackDmInboundScanner
from teatree.loop.slack_answer.cycle import run_slack_answer_cycle
from tests.integration.slack_bridge_e2e.conftest import FakeSlackTransport

pytestmark = [pytest.mark.django_db, pytest.mark.integration]

_ROOT_TS = "1700000000.0001"
_REPLY_TS = "1700000000.0002"


class TestThreadReplyEyesReaction:
    """User replies in-thread to a bot DM → loop reacts :eyes: on the reply.

    The thread root is the bot's own top-level message; the user's reply
    carries its own ``ts``. The reactive cycle must react on the reply's
    ``ts`` exactly as it does for a top-level DM — the gap reported in
    #1366 was the :eyes: receipt never landing on thread replies.
    """

    def _seed_thread_reply(self, transport: FakeSlackTransport) -> SlackBotBackend:
        transport.default_responses["conversations.history"] = {
            "ok": True,
            "messages": [
                {
                    "ts": _ROOT_TS,
                    "thread_ts": _ROOT_TS,
                    "user": "B_BOT",
                    "text": "bot top-level notify",
                },
            ],
        }
        transport.default_responses["conversations.replies"] = {
            "ok": True,
            "messages": [
                {"ts": _ROOT_TS, "user": "B_BOT", "text": "bot top-level notify"},
                {"ts": _REPLY_TS, "thread_ts": _ROOT_TS, "user": "U_HUMAN", "text": "thanks"},
            ],
        }
        return SlackBotBackend(bot_token="xoxb-bot", user_id="U_HUMAN")

    def test_reactions_add_called_on_thread_reply_ts(self, transport: FakeSlackTransport) -> None:
        backend = self._seed_thread_reply(transport)
        SlackDmInboundScanner(backend=backend, overlay="demo").scan()

        run_slack_answer_cycle(messaging_resolver=lambda _overlay: backend)

        eyes_calls = [c for c in transport.calls_to("reactions.add") if c.payload.get("name") == "eyes"]
        reacted_ts = [c.payload.get("timestamp") for c in eyes_calls]
        assert reacted_ts == [_REPLY_TS]

    def test_thread_reply_row_marked_eyes_reacted(self, transport: FakeSlackTransport) -> None:
        backend = self._seed_thread_reply(transport)
        SlackDmInboundScanner(backend=backend, overlay="demo").scan()

        run_slack_answer_cycle(messaging_resolver=lambda _overlay: backend)

        reply_row = PendingChatInjection.objects.get(slack_ts=_REPLY_TS)
        assert reply_row.eyes_reacted_at is not None
