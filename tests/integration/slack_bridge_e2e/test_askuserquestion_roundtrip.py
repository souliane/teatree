"""Owner-question Slack roundtrip: record → mirror → reply match → answer applied (#1174, #5096).

The full bridge end to end with only the network mocked:

- an owner decision is recorded with ``questions record --decision`` and the
first-post drain delivers it through the one ``notify_user`` egress, stamping
the mirror (a loop-driven ``AskUserQuestion`` is internal and never reaches Slack);
- the user's Slack reply is polled into a ``PendingChatInjection`` row by
the real ``SlackDmInboundScanner``;
- the real ``AskUserQuestionReplyScanner`` binds the reply to the live
question, applies it, and reacts ✅; the answer is then readable on demand.
"""

import io
import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from django.core.management import call_command

from teatree.backends.slack.bot import SlackBotBackend
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.core.notify_question_drains import drain_unmirrored_deferred_questions
from teatree.loop.scanners.askuserquestion_reply import AskUserQuestionReplyScanner
from teatree.loops.slack_inbound import catch_up_on_connect
from tests.integration.slack_bridge_e2e.conftest import FakeSlackTransport, _own_loop, serve_history

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = [pytest.mark.django_db, pytest.mark.integration]

_OPTIONS = json.dumps(
    [
        {"label": "staging", "description": "I deploy to the test environment first.", "recommended": True},
        {"label": "prod", "description": "I deploy to the live environment."},
    ]
)


class TestAskUserQuestionRoundtrip:
    def test_record_then_reply_then_apply(
        self,
        transport: FakeSlackTransport,
        overlay_backend: SlackBotBackend,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _own_loop("s-loop", monkeypatch, tmp_path)
        backend = SlackBotBackend(bot_token="xoxb-bot", user_id="U_HUMAN")

        call_command(
            "questions",
            "record",
            "Which env?",
            "--decision",
            "architecture",
            "--checked",
            "The ticket names no target environment.",
            "--why",
            "The deploy needs a target before it can start.",
            "--blocker",
            "Only the owner picks the environment.",
            "--options",
            _OPTIONS,
        )
        drain_unmirrored_deferred_questions(user_id="U_HUMAN", backend=backend)
        question = DeferredQuestion.objects.get()
        assert question.slack_ts != "", "the first-post drain did not deliver the question"
        assert question.slack_channel == "D-USER"

        serve_history(transport, [{"ts": f"{time.time() - 30:.6f}", "user": "U_HUMAN", "text": "1"}])
        catch_up_on_connect("", time.time() - 120)

        with patch.object(SlackBotBackend, "_is_self_dm", return_value=True):
            AskUserQuestionReplyScanner(backend=backend, overlay="").scan()

        question.refresh_from_db()
        assert question.answer_text == "staging"
        assert question.resolved_via == "slack"

        listed = io.StringIO()
        call_command("questions", "list", "--all", "--json", stdout=listed)
        assert {"id": question.pk, "answer": "staging"}.items() <= json.loads(listed.getvalue())[0].items()
