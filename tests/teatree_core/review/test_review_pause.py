"""Pause is read LIVE off the root message's reactions, and every failure is UNKNOWN.

The user holds a review request by reacting to its Slack message with a pause
emoji — "I have more to fix, do not count this as ready". Nothing is persisted:
the reaction is the state, so lifting it must resume the request with no
bookkeeping to undo.

Which makes the failure direction the whole safety property. A transport that
errors, a payload that comes back empty, and a caller with no messaging backend
at all are indistinguishable from "the owner has not paused it" unless the
reader refuses to answer — so each is asserted UNKNOWN, never NOT_PAUSED, and
each assertion carries BOTH green controls inline (a genuine PAUSED and a
genuine NOT_PAUSED on the same post). Without those controls an UNKNOWN cannot
be told apart from a fake that simply returns nothing.
"""

from django.test import TestCase

from teatree.core.models import ConfigSetting, ReviewRequestPost
from teatree.core.review.review_pause import PauseState, read_pause_state
from teatree.types import RawAPIDict

_CHANNEL = "C_REVIEW"
_THREAD_TS = "1700000000.000100"
_DEFAULT_PAUSE_EMOJI = "double_vertical_bar"


class _Messaging:
    """A messaging backend whose root-message read always succeeds."""

    def __init__(self, message: RawAPIDict) -> None:
        self.message = message
        self.reads: list[tuple[str, str]] = []

    def fetch_message(self, *, channel: str, ts: str) -> RawAPIDict:
        self.reads.append((channel, ts))
        return self.message


class _BrokenMessaging:
    """A messaging backend whose root-message read raises — the transport is down."""

    def __init__(self) -> None:
        self.reads: list[tuple[str, str]] = []

    def fetch_message(self, *, channel: str, ts: str) -> RawAPIDict:
        self.reads.append((channel, ts))
        msg = "slack transport unreachable"
        raise RuntimeError(msg)


def _message_with(*reaction_names: str) -> RawAPIDict:
    return {
        "ts": _THREAD_TS,
        "text": "Review please: https://git.example.com/acme/app/-/merge_requests/41",
        "reactions": [{"name": name, "users": ["U_OWNER"], "count": 1} for name in reaction_names],
    }


def _post(*, mr_id: int = 41, channel: str = _CHANNEL, thread_ts: str = _THREAD_TS) -> ReviewRequestPost:
    return ReviewRequestPost.objects.create(
        mr_url=f"https://git.example.com/acme/app/-/merge_requests/{mr_id}",
        slack_channel_id=channel,
        slack_thread_ts=thread_ts,
    )


class TestOnlyAConfiguredEmojiPauses(TestCase):
    def test_a_shipped_pause_reaction_reads_paused(self) -> None:
        assert read_pause_state(_post(), _Messaging(_message_with(_DEFAULT_PAUSE_EMOJI))) is PauseState.PAUSED

    def test_an_unrelated_reaction_reads_not_paused(self) -> None:
        assert read_pause_state(_post(), _Messaging(_message_with("eyes"))) is PauseState.NOT_PAUSED

    def test_a_message_carrying_no_reactions_at_all_reads_not_paused(self) -> None:
        """A real payload with no ``reactions`` key is a genuine empty, not a failure."""
        message: RawAPIDict = {"ts": _THREAD_TS, "text": "Review please"}

        assert read_pause_state(_post(), _Messaging(message)) is PauseState.NOT_PAUSED

    def test_the_configured_list_is_what_decides_not_a_hard_coded_name(self) -> None:
        ConfigSetting.objects.set_value("review_pause_reaction_emojis", ["on_hold"])
        post = _post()

        assert read_pause_state(post, _Messaging(_message_with("on_hold"))) is PauseState.PAUSED
        assert read_pause_state(post, _Messaging(_message_with(_DEFAULT_PAUSE_EMOJI))) is PauseState.NOT_PAUSED

    def test_one_configured_emoji_among_several_reactions_pauses(self) -> None:
        message = _message_with("eyes", "rocket", _DEFAULT_PAUSE_EMOJI)

        assert read_pause_state(_post(), _Messaging(message)) is PauseState.PAUSED


class TestTransportFailureIsUnknownNeverNotPaused(TestCase):
    """Fail closed: three distinct failures, each with both green controls inline."""

    def test_a_raising_backend_is_unknown(self) -> None:
        post = _post()
        broken = _BrokenMessaging()

        assert read_pause_state(post, broken) is PauseState.UNKNOWN
        # Controls: the same post reads BOTH states correctly on a live backend,
        # so the UNKNOWN above is the refusal and not an inert reader.
        assert read_pause_state(post, _Messaging(_message_with(_DEFAULT_PAUSE_EMOJI))) is PauseState.PAUSED
        assert read_pause_state(post, _Messaging(_message_with("eyes"))) is PauseState.NOT_PAUSED
        assert broken.reads == [(_CHANNEL, _THREAD_TS)]

    def test_an_empty_payload_is_unknown(self) -> None:
        post = _post()

        assert read_pause_state(post, _Messaging({})) is PauseState.UNKNOWN
        assert read_pause_state(post, _Messaging(_message_with(_DEFAULT_PAUSE_EMOJI))) is PauseState.PAUSED
        assert read_pause_state(post, _Messaging(_message_with("eyes"))) is PauseState.NOT_PAUSED

    def test_no_messaging_backend_is_unknown(self) -> None:
        post = _post()

        assert read_pause_state(post, None) is PauseState.UNKNOWN
        assert read_pause_state(post, _Messaging(_message_with(_DEFAULT_PAUSE_EMOJI))) is PauseState.PAUSED
        assert read_pause_state(post, _Messaging(_message_with("eyes"))) is PauseState.NOT_PAUSED

    def test_a_post_with_no_slack_coordinates_is_unknown_and_never_reaches_the_backend(self) -> None:
        messaging = _Messaging(_message_with("eyes"))

        assert read_pause_state(_post(mr_id=51, thread_ts=""), messaging) is PauseState.UNKNOWN
        assert read_pause_state(_post(mr_id=52, channel=""), messaging) is PauseState.UNKNOWN
        assert messaging.reads == []
        # Control: the same backend answers a fully-addressed post.
        assert read_pause_state(_post(mr_id=53), messaging) is PauseState.NOT_PAUSED


class TestPauseIsNeverPersisted(TestCase):
    def test_reading_the_state_writes_nothing_to_the_post(self) -> None:
        """Lifting the reaction must resume the request, so no cached flag may exist."""
        post = _post()
        before = ReviewRequestPost.objects.values().get(pk=post.pk)

        read_pause_state(post, _Messaging(_message_with(_DEFAULT_PAUSE_EMOJI)))
        read_pause_state(post, _Messaging(_message_with("eyes")))

        assert ReviewRequestPost.objects.values().get(pk=post.pk) == before

    def test_a_lifted_reaction_immediately_reads_not_paused_again(self) -> None:
        post = _post()

        assert read_pause_state(post, _Messaging(_message_with(_DEFAULT_PAUSE_EMOJI))) is PauseState.PAUSED
        assert read_pause_state(post, _Messaging(_message_with())) is PauseState.NOT_PAUSED
