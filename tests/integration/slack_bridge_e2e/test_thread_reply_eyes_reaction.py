"""Thread-reply :eyes: receipt end-to-end (#1366).

The inbound-to-reactive chain for a user message that arrives as a Slack
*thread reply* (not a top-level DM): the bot posts a top-level message,
the user replies in-thread, and the reactive Slack-answer loop must stamp
the same :eyes: receipt it stamps on a top-level DM. The whole chain runs
unmodified — the real downtime catch-up (with its ``conversations.replies``
walk), the real recorder, real ``PendingChatInjection`` rows, real
``run_slack_answer_cycle`` — with only the ``httpx`` network faked. The load-bearing assertion is that
``reactions.add`` is called on the *thread reply's* ``ts``, not the thread
root's.
"""

import time

import pytest

from teatree.backends.slack.bot import SlackBotBackend
from teatree.core.models import PendingChatInjection
from teatree.loop.slack_answer.cycle import run_slack_answer_cycle
from teatree.loops.slack_inbound import catch_up_on_connect
from tests.integration.slack_bridge_e2e.conftest import FakeSlackTransport, serve_history

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = [pytest.mark.django_db, pytest.mark.integration]


class TestThreadReplyEyesReaction:
    """User replies in-thread to a bot DM → loop reacts :eyes: on the reply.

    The thread root is the bot's own top-level message; the user's reply
    carries its own ``ts``. The reactive cycle must react on the reply's
    ``ts`` exactly as it does for a top-level DM — the gap reported in
    #1366 was the :eyes: receipt never landing on thread replies.
    """

    def _catch_up_thread_reply(self, transport: FakeSlackTransport) -> str:
        root, reply = f"{time.time() - 60:.6f}", f"{time.time() - 30:.6f}"
        serve_history(
            transport,
            [{"ts": root, "user": "B_BOT", "text": "bot top-level notify"}],
            replies={root: [{"ts": reply, "user": "U_HUMAN", "text": "thanks"}]},
        )
        catch_up_on_connect("demo", time.time() - 120)
        return reply

    def test_reactions_add_called_on_thread_reply_ts(
        self, transport: FakeSlackTransport, overlay_backend: SlackBotBackend
    ) -> None:
        reply = self._catch_up_thread_reply(transport)

        run_slack_answer_cycle(messaging_resolver=lambda _overlay: overlay_backend)

        eyes_calls = [c for c in transport.calls_to("reactions.add") if c.payload.get("name") == "eyes"]
        assert [c.payload.get("timestamp") for c in eyes_calls] == [reply]

    def test_thread_reply_row_marked_eyes_reacted(
        self, transport: FakeSlackTransport, overlay_backend: SlackBotBackend
    ) -> None:
        reply = self._catch_up_thread_reply(transport)

        run_slack_answer_cycle(messaging_resolver=lambda _overlay: overlay_backend)

        assert PendingChatInjection.objects.get(slack_ts=reply).eyes_reacted_at is not None
