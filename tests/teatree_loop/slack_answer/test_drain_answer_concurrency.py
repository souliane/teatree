"""The reactive answer cycle is complementary to the prompt-drain (#1014/#1075).

``consume()`` (the ``UserPromptSubmit`` prompt-drain) stamps
``consumed_at``; the reactive cycle stamps ``loop_replied_at`` /
``eyes_reacted_at`` (its own columns, deliberately distinct from
#1069's ``answered_at`` turn-end gate — Option B). These are orthogonal
single-use CAS transitions on different columns, so draining and
loop-replying the SAME row — even interleaved — sets both independently
with no exception and no double-reply / double-drain.
"""

from dataclasses import dataclass, field
from unittest.mock import patch

import pytest

from teatree.core.models import PendingChatInjection
from teatree.loop.slack_answer.cycle import run_slack_answer_cycle
from teatree.types import RawAPIDict

pytestmark = pytest.mark.django_db


_BOT_UID = "UBOT"


@dataclass
class RecordingBackend:
    reactions: list[tuple[str, str, str]] = field(default_factory=list)
    replies: list[tuple[str, str, str]] = field(default_factory=list)
    thread_replies: dict[str, list[RawAPIDict]] = field(default_factory=dict)

    def fetch_mentions(self, *, since: str = "") -> list[RawAPIDict]:
        _ = since
        return []

    def fetch_dms(self, *, since: str = "") -> list[RawAPIDict]:
        _ = since
        return []

    def fetch_message(self, *, channel: str, ts: str) -> RawAPIDict:
        _ = (channel, ts)
        return {}

    def fetch_thread_replies(self, *, channel: str, thread_ts: str) -> list[RawAPIDict]:
        _ = channel
        return list(self.thread_replies.get(thread_ts, []))

    def auth_test(self) -> RawAPIDict:
        return {"ok": True, "user_id": _BOT_UID}

    def post_message(self, *, channel: str, text: str, thread_ts: str = "") -> RawAPIDict:
        _ = (channel, text, thread_ts)
        return {}

    def post_reply(self, *, channel: str, ts: str, text: str) -> RawAPIDict:
        self.replies.append((channel, ts, text))
        self.thread_replies.setdefault(ts, []).append({"ts": f"{ts}-bot", "user": _BOT_UID, "text": text})
        return {"ok": True}

    def open_dm(self, user_id: str) -> str:
        _ = user_id
        return "D1"

    def get_permalink(self, *, channel: str, ts: str) -> str:
        _ = (channel, ts)
        return "https://slack/p1"

    def react(self, *, channel: str, ts: str, emoji: str) -> RawAPIDict:
        self.reactions.append((channel, ts, emoji))
        return {"ok": True}

    def resolve_user_id(self, handle: str) -> str:
        _ = handle
        return ""


class TestDrainAnswerOrthogonality:
    def test_drain_then_answer_sets_both_columns(self) -> None:
        row = PendingChatInjection.record(channel="C1", slack_ts="1.0", text="thanks!")
        assert row is not None

        # Prompt-drain runs first (consume == the drain's column write).
        assert row.consume() is True
        backend = RecordingBackend()

        # The reactive cycle still picks the row up (loop_unreplied() gates
        # on loop_replied_at, not consumed_at) and replies independently.
        report = run_slack_answer_cycle(messaging_resolver=lambda _o: backend)

        row.refresh_from_db()
        assert row.consumed_at is not None
        assert row.loop_replied_at is not None
        assert row.answer_kind == "ack"
        assert report.acked == 1

    def test_answer_then_drain_sets_both_columns(self) -> None:
        row = PendingChatInjection.record(channel="C1", slack_ts="1.0", text="thanks!")
        assert row is not None
        backend = RecordingBackend()

        run_slack_answer_cycle(messaging_resolver=lambda _o: backend)
        # Drain runs AFTER the answer — still a clean single-use transition.
        fresh = PendingChatInjection.objects.get(pk=row.pk)
        assert fresh.consume() is True

        fresh.refresh_from_db()
        assert fresh.loop_replied_at is not None
        assert fresh.consumed_at is not None

    def test_no_double_reply_across_drain_and_answer_reruns(self) -> None:
        row = PendingChatInjection.record(channel="C1", slack_ts="1.0", text="what's the status?")
        assert row is not None
        backend = RecordingBackend()
        with patch(
            "teatree.loop.slack_answer.simple_answer.statusline_for_slack",
            return_value="overlay=acme\nticket=#1\n",
        ):
            run_slack_answer_cycle(messaging_resolver=lambda _o: backend)
            row.refresh_from_db()
            row.consume()  # interleave the drain between two cycles
            run_slack_answer_cycle(messaging_resolver=lambda _o: backend)

        # Exactly ONE thread reply despite drain + two answer cycles.
        assert len(backend.replies) == 1
