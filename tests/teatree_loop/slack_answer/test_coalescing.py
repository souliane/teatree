"""Message coalescing — consecutive follow-ups are one logical turn (#1014).

Real example: two Slack messages 3s apart - same logical request coalesced.
The loop must concatenate consecutive same-user/channel rows with no bot
reply between them and within the window into ONE unit: classify once,
thread on the FIRST ts, :eyes: + answer ALL rows together.
Zero-token — pure DB/time logic.
"""

from dataclasses import dataclass, field
from unittest.mock import patch

import pytest
from django.utils import timezone

from teatree.core.models import PendingChatInjection
from teatree.loop.slack_answer.cycle import _COALESCE_WINDOW_SECONDS, _coalesce, run_slack_answer_cycle
from teatree.types import RawAPIDict

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


@dataclass
class RecordingBackend:
    reactions: list[tuple[str, str, str]] = field(default_factory=list)
    replies: list[tuple[str, str, str]] = field(default_factory=list)

    def fetch_mentions(self, *, since: str = "") -> list[RawAPIDict]:
        _ = since
        return []

    def fetch_dms(self, *, since: str = "") -> list[RawAPIDict]:
        _ = since
        return []

    def post_message(self, *, channel: str, text: str, thread_ts: str = "") -> RawAPIDict:
        _ = (channel, text, thread_ts)
        return {}

    def post_reply(self, *, channel: str, ts: str, text: str) -> RawAPIDict:
        self.replies.append((channel, ts, text))
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


def _seed(ts: str, text: str, *, user: str, channel: str, secs_after_base: float) -> PendingChatInjection:
    row = PendingChatInjection.record(channel=channel, slack_ts=ts, text=text, user_id=user)
    assert row is not None
    base = timezone.now().replace(microsecond=0)
    row.received_at = base + timezone.timedelta(seconds=secs_after_base)
    row.save(update_fields=["received_at"])
    return row


class TestCoalesceGrouping:
    def test_two_messages_3s_apart_no_bot_between_is_one_unit(self) -> None:
        _seed("1.0", "please bump my review requests", user="U1", channel="C1", secs_after_base=0)
        _seed("2.0", "there are several", user="U1", channel="C1", secs_after_base=3)

        units = _coalesce(list(PendingChatInjection.loop_unreplied()))

        assert len(units) == 1
        assert units[0].slack_ts == "1.0"  # threads on the FIRST message
        assert units[0].text == "please bump my review requests\nthere are several"
        assert len(units[0].rows) == 2

    def test_gap_greater_than_window_is_two_units(self) -> None:
        _seed("1.0", "first", user="U1", channel="C1", secs_after_base=0)
        _seed("2.0", "much later", user="U1", channel="C1", secs_after_base=_COALESCE_WINDOW_SECONDS + 5)

        units = _coalesce(list(PendingChatInjection.loop_unreplied()))

        assert len(units) == 2

    def test_different_user_is_never_coalesced(self) -> None:
        _seed("1.0", "mine", user="U1", channel="C1", secs_after_base=0)
        _seed("2.0", "theirs", user="U2", channel="C1", secs_after_base=2)

        units = _coalesce(list(PendingChatInjection.loop_unreplied()))

        assert len(units) == 2

    def test_different_channel_is_never_coalesced(self) -> None:
        _seed("1.0", "here", user="U1", channel="C1", secs_after_base=0)
        _seed("2.0", "there", user="U1", channel="C2", secs_after_base=2)

        units = _coalesce(list(PendingChatInjection.loop_unreplied()))

        assert len(units) == 2

    def test_blank_user_id_is_never_coalesced(self) -> None:
        # No author attribution → cannot prove same author.
        PendingChatInjection.record(channel="C1", slack_ts="1.0", text="a")
        PendingChatInjection.record(channel="C1", slack_ts="2.0", text="b")

        units = _coalesce(list(PendingChatInjection.loop_unreplied()))

        assert len(units) == 2

    def test_bot_reply_between_breaks_the_group(self) -> None:
        # The first message is answered (a bot reply posted) BEFORE the
        # follow-up arrives; the follow-up is then its own logical turn.
        first = _seed("1.0", "status?", user="U1", channel="C1", secs_after_base=0)
        first.mark_loop_replied("simple")  # bot replied → group boundary
        _seed("2.0", "and the PRs?", user="U1", channel="C1", secs_after_base=3)

        # unanswered() now only returns the follow-up — the answered lead
        # is no longer a candidate, so the follow-up stands alone.
        units = _coalesce(list(PendingChatInjection.loop_unreplied()))

        assert len(units) == 1
        assert units[0].slack_ts == "2.0"
        assert len(units[0].rows) == 1


class TestCoalescedAnswerBehaviour:
    def test_all_coalesced_rows_eyes_reacted_and_answered_together(self) -> None:
        r1 = _seed("1.0", "thanks", user="U1", channel="C1", secs_after_base=0)
        r2 = _seed("2.0", "really appreciate it", user="U1", channel="C1", secs_after_base=2)
        backend = RecordingBackend()

        report = run_slack_answer_cycle(messaging_resolver=lambda _o: backend)

        # :eyes: on BOTH rows.
        eyes_ts = {ts for _c, ts, emoji in backend.reactions if emoji == "eyes"}
        assert eyes_ts == {"1.0", "2.0"}
        # Both rows stamped answered together; processed counts rows.
        r1.refresh_from_db()
        r2.refresh_from_db()
        assert r1.loop_replied_at is not None
        assert r2.loop_replied_at is not None
        assert report.processed == 2
        assert report.acked == 1  # ONE logical turn

    def test_coalesced_simple_threads_on_first_ts_only(self) -> None:
        _seed("1.0", "what's the status", user="U1", channel="C1", secs_after_base=0)
        _seed("2.0", "and pending too", user="U1", channel="C1", secs_after_base=2)
        backend = RecordingBackend()
        with patch(
            "teatree.loop.slack_answer.simple_answer.statusline_for_slack",
            return_value="overlay=acme\nticket=#1\n",
        ):
            run_slack_answer_cycle(messaging_resolver=lambda _o: backend)

        assert len(backend.replies) == 1
        assert backend.replies[0][1] == "1.0"  # FIRST message's ts
