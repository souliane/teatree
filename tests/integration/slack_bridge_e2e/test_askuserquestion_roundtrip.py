"""AskUserQuestion Slack roundtrip: capture(deny) → reply match → answer applied (#1174).

The full bridge end to end with only the network mocked:

- a loop-driven ``AskUserQuestion`` is captured as an un-mirrored
``DeferredQuestion``, the tool call is denied, and the capture-time kick
delivers it through the one ``notify_user`` egress, stamping the mirror;
- the user's Slack reply is polled into a ``PendingChatInjection`` row by
the real ``SlackDmInboundScanner``;
- the real ``AskUserQuestionReplyScanner`` binds the reply to the live
question, applies it, and reacts ✅;
- the next ``UserPromptSubmit`` injects the answer and stamps
``applied_at`` so it surfaces exactly once.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

import hooks.scripts.hook_router as router
from teatree.backends.slack.bot import SlackBotBackend
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.core.notify_question_drains import drain_unmirrored_deferred_questions
from teatree.loop.scanners.askuserquestion_reply import AskUserQuestionReplyScanner
from teatree.loop.scanners.slack_dm_inbound import SlackDmInboundScanner
from tests.integration.slack_bridge_e2e.conftest import FakeSlackTransport, _own_loop

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = [pytest.mark.django_db, pytest.mark.integration]

_QUESTION = {
    "tool_name": "AskUserQuestion",
    "tool_input": {"questions": [{"question": "Which env?", "options": [{"label": "staging"}, {"label": "prod"}]}]},
    "session_id": "s-loop",
}


class TestAskUserQuestionRoundtrip:
    def test_capture_then_reply_then_apply(
        self,
        transport: FakeSlackTransport,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _own_loop("s-loop", monkeypatch, tmp_path)
        monkeypatch.setattr(router, "_is_live_user_turn", lambda _data: False)
        backend = SlackBotBackend(bot_token="xoxb-bot", user_id="U_HUMAN")
        # The kick is detached in production; run the SAME drain inline so the test
        # exercises the real egress rather than a stand-in for it.
        monkeypatch.setattr(
            router,
            "_kick_question_drain",
            lambda ref: drain_unmirrored_deferred_questions(user_id="U_HUMAN", only_ref=ref, backend=backend),
        )

        verdict = router.handle_mirror_question_to_slack(dict(_QUESTION))
        assert verdict is True
        capsys.readouterr()
        question = DeferredQuestion.objects.get()
        assert question.slack_ts != "", "the capture-time kick did not deliver the question"
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
        assert question.applied_at is None

        router.handle_inject_pending_questions({"session_id": "s-loop"})
        out = capsys.readouterr().out
        assert f"#{question.pk}" in out
        assert "staging" in out
        question.refresh_from_db()
        assert question.applied_at is not None
