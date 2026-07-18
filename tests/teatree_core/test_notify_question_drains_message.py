"""The DeferredQuestion resurface DM must be Slack-reply-only, never a host CLI.

The owner reads Slack DMs and has NO host-CLI access — every interaction is in
Slack. The resurface/mirror message the drains post therefore must NOT tell the
owner to run ``t3 <overlay> questions answer …``; the owner just replies in the
thread and the reply scanner binds the answer.
"""

import json

from django.test import TestCase

from teatree.core.models import DeferredQuestion
from teatree.core.notify_question_drains import _resurface_text


class TestResurfaceMessageHasNoHostCli(TestCase):
    def test_message_carries_no_t3_cli_instruction(self) -> None:
        row = DeferredQuestion.record(question="Should I merge PR #7?", session_id="s1")

        text = _resurface_text(row)

        # No host-CLI instruction of any kind — the owner cannot run one.
        assert "t3 " not in text
        assert "questions answer" not in text
        assert "Answer with" not in text
        # It DOES tell the owner to reply in the thread instead.
        assert "reply" in text.lower()
        assert "thread" in text.lower()

    def test_message_still_renders_question_and_options(self) -> None:
        row = DeferredQuestion.record(
            question="Pick a rollout",
            options_json=json.dumps([{"label": "canary", "description": "10% first"}]),
            session_id="s2",
        )

        text = _resurface_text(row)

        assert "Pick a rollout" in text
        assert "canary" in text
        assert "t3 " not in text
