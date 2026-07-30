"""The DeferredQuestion resurface DM must be Slack-reply-only, never a host CLI.

The owner reads Slack DMs and has NO host-CLI access — every interaction is in
Slack. The resurface/mirror message the drains post therefore must NOT tell the
owner to run ``t3 <overlay> questions answer …``; the owner just replies in the
thread and the reply scanner binds the answer.
"""

import json
from unittest.mock import patch

from django.test import TestCase

from teatree.core.models import DeferredQuestion
from teatree.core.notify_question_drains import _resurface_text, drain_deferred_questions


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


class TestDrainExcludesInternalAudience(TestCase):
    """An INTERNAL row (an agent tool-lack self-report) never joins the owner DM batch."""

    def test_internal_only_backlog_drains_nothing(self) -> None:
        DeferredQuestion.record(
            "This session lacks any shell/write tool to run record_candidate.",
            audience=DeferredQuestion.Audience.INTERNAL,
        )
        with patch("teatree.core.notify_question_drains.notify_user") as notify:
            delivered, total = drain_deferred_questions()
        # The INTERNAL row is filtered before any egress — notify_user is never called.
        notify.assert_not_called()
        assert (delivered, total) == (0, 0)

    def test_owner_row_drains_but_internal_peer_is_excluded(self) -> None:
        owner = DeferredQuestion.record("Should I merge PR #7?")
        DeferredQuestion.record(
            "I run shell-denied and cannot file the issue.",
            audience=DeferredQuestion.Audience.INTERNAL,
        )
        with patch("teatree.core.notify_question_drains.notify_user", return_value=True) as notify:
            delivered, total = drain_deferred_questions()
        # Exactly one egress — the owner row — and the total counts only it.
        assert notify.call_count == 1
        assert owner.question in notify.call_args.args[0]
        assert (delivered, total) == (1, 1)
