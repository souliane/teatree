"""A reply claim survives only an application that actually happened.

``AskUserQuestionReplyScanner`` claims the reply, acknowledges it, then applies
the answer — and releases the claim when the apply *returns* a refusal. An apply
that *raised* skipped that release: the scanner's per-reply guard logged and
moved on, leaving a reply marked answered against a question still pending, with
no later cycle able to pick either of them up.
"""

import hashlib
import json
from dataclasses import dataclass, field
from unittest.mock import patch

from django.test import TestCase

from teatree.core.models import PendingChatInjection
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.core.models.pending_chat_injection import DmContext
from teatree.loop.scanners.askuserquestion_reply import AskUserQuestionReplyScanner
from teatree.types import RawAPIDict

_CHANNEL = "D-user"
_OPTIONS = [{"label": "Yes"}, {"label": "No"}]


@dataclass
class FakeMessaging:
    """Self-DM messaging surface — every react/post is ungated."""

    route_token: str = "self"
    react_calls: list[tuple[str, str, str]] = field(default_factory=list)

    def _is_self_dm(self, channel: str) -> bool:
        _ = channel
        return True

    def react_routed(self, *, channel: str, ts: str, emoji: str) -> RawAPIDict:
        self.react_calls.append((channel, ts, emoji))
        return {"ok": True}

    def post_routed(self, *, channel: str, text: str, thread_ts: str = "") -> RawAPIDict:
        _ = (channel, text, thread_ts)
        return {"ok": True}

    def get_permalink(self, *, channel: str, ts: str) -> str:
        _ = (channel, ts)
        return "https://slack/permalink"


def _options_hash(options: list[dict]) -> str:
    return hashlib.sha256(json.dumps(options, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


class TestClaimReleasedWhenApplyRaises(TestCase):
    """A raising ``apply_answer`` leaves the reply reclaimable by the next cycle."""

    def setUp(self) -> None:
        self.question = DeferredQuestion.record(
            "Ship it?",
            options_json=json.dumps(_OPTIONS),
            options_hash=_options_hash(_OPTIONS),
            session_id="s",
            run_id="r",
            generation=1,
            slack_channel=_CHANNEL,
            slack_ts="100.0",
        )
        reply = PendingChatInjection.record(
            channel=_CHANNEL, slack_ts="200.0", text="1", context=DmContext(user_id="U1")
        )
        assert reply is not None
        self.reply = reply

    def test_raising_apply_releases_the_claim(self) -> None:
        backend = FakeMessaging()

        with patch.object(DeferredQuestion, "apply_answer", side_effect=RuntimeError("apply blew up")):
            AskUserQuestionReplyScanner(backend=backend, overlay="").scan()

        self.reply.refresh_from_db()
        self.question.refresh_from_db()
        assert backend.react_calls == [(_CHANNEL, "200.0", "white_check_mark")], (
            "the reply must actually have been claimed and acked — otherwise nothing is being released"
        )
        assert self.reply.loop_replied_at is None, "an un-applied answer must not consume the reply"
        assert self.question.is_pending is True
