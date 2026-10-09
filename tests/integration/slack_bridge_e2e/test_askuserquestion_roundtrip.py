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
from pathlib import Path
from unittest.mock import patch

import pytest
from django.core.management import call_command

from teatree.backends.slack.bot import SlackBotBackend
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.core.notify_question_drains import drain_unmirrored_deferred_questions
from teatree.loop.scanners.askuserquestion_reply import AskUserQuestionReplyScanner
from teatree.loop.scanners.slack_dm_inbound import SlackDmInboundScanner
from tests.integration.slack_bridge_e2e.conftest import FakeSlackTransport, _own_loop

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = [pytest.mark.django_db, pytest.mark.integration]

_OPTIONS = json.dumps([{"label": "staging"}, {"label": "prod"}])


class TestAskUserQuestionRoundtrip:
    def test_record_then_reply_then_apply(
        self,
        transport: FakeSlackTransport,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _own_loop("s-loop", monkeypatch, tmp_path)
        backend = SlackBotBackend(bot_token="xoxb-bot", user_id="U_HUMAN")

        call_command("questions", "record", "Which env?", "--decision", "product_scope", "--options", _OPTIONS)
        drain_unmirrored_deferred_questions(user_id="U_HUMAN", backend=backend)
        question = DeferredQuestion.objects.get()
        assert question.slack_ts != "", "the first-post drain did not deliver the question"
        assert question.slack_channel == "D-USER"

        transport.default_responses["conversations.history"] = {
            "ok": True,
            "messages": [{"ts": "1700000000.0050", "user": "U_HUMAN", "channel": "D-USER", "text": "1"}],
        }
        SlackDmInboundScanner(backend=backend, overlay="").scan()

        with patch.object(SlackBotBackend, "_is_self_dm", return_value=True):
            AskUserQuestionReplyScanner(backend=backend, overlay="").scan()

        question.refresh_from_db()
        assert question.answer_text == "staging"
        assert question.resolved_via == "slack"

        listed = io.StringIO()
        call_command("questions", "list", "--all", "--json", stdout=listed)
        assert {"id": question.pk, "answer": "staging"}.items() <= json.loads(listed.getvalue())[0].items()
