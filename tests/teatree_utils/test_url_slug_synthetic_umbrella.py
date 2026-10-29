"""The synthetic-loop umbrella anchor predicate (souliane/teatree#3706).

Every synthetic loop ticket — directive interpret/implement, outer-loop experiment —
anchors on ONE umbrella issue and disambiguates via a URL fragment. The predicate
recognises that anchor (any fragment, or none) so the artifact-terminal task sweep can
skip these FSM-owned tasks regardless of their phase.
"""

import pytest

from teatree.core.models.ticket_number import derive_issue_number
from teatree.utils.url_slug import (
    SYNTHETIC_LOOP_UMBRELLA_URL,
    conversation_anchor,
    is_conversation_anchor,
    is_synthetic_loop_umbrella_url,
    slack_conversation_anchor,
)


class TestIsSyntheticLoopUmbrellaUrl:
    def test_bare_umbrella_matches(self) -> None:
        assert is_synthetic_loop_umbrella_url(SYNTHETIC_LOOP_UMBRELLA_URL)

    def test_interpret_fragment_matches(self) -> None:
        assert is_synthetic_loop_umbrella_url(f"{SYNTHETIC_LOOP_UMBRELLA_URL}#directive=5")

    def test_implement_fragment_matches(self) -> None:
        assert is_synthetic_loop_umbrella_url(f"{SYNTHETIC_LOOP_UMBRELLA_URL}#directive-impl=5")

    def test_outer_loop_fragment_matches(self) -> None:
        assert is_synthetic_loop_umbrella_url(f"{SYNTHETIC_LOOP_UMBRELLA_URL}#outer-loop-experiment=7")

    def test_a_real_issue_does_not_match(self) -> None:
        assert not is_synthetic_loop_umbrella_url("https://github.com/souliane/teatree/issues/42")

    def test_a_numeric_superstring_of_the_umbrella_does_not_match(self) -> None:
        # #30091 startswith #3009 textually — the base must be an EXACT match, not a prefix.
        assert not is_synthetic_loop_umbrella_url("https://github.com/souliane/teatree/issues/30091")

    def test_empty_url_does_not_match(self) -> None:
        assert not is_synthetic_loop_umbrella_url("")


class TestSlackConversationAnchor:
    """Each load-bearing component of the anchor, pinned (#4527)."""

    def test_the_channel_disambiguates_the_same_ts_across_channels(self) -> None:
        # Slack ts is unique per channel, not globally: dropping the channel collapses
        # two unrelated conversations onto one row under `unique_nonempty_issue_url`.
        assert slack_conversation_anchor(channel="D-owner", slack_ts="1.0") != slack_conversation_anchor(
            channel="C-team", slack_ts="1.0"
        )

    def test_the_ts_disambiguates_two_messages_in_one_channel(self) -> None:
        assert slack_conversation_anchor(channel="D-owner", slack_ts="1.0") != slack_conversation_anchor(
            channel="D-owner", slack_ts="2.0"
        )

    def test_the_anchor_is_recognised_as_the_synthetic_umbrella(self) -> None:
        assert is_synthetic_loop_umbrella_url(slack_conversation_anchor(channel="D-owner", slack_ts="1.0"))

    def test_the_trailing_suffix_stops_the_ts_reading_as_an_issue_number(self) -> None:
        anchor = slack_conversation_anchor(channel="D-owner", slack_ts="1730000000.123456")

        assert derive_issue_number(anchor) != "123456"


class TestConversationAnchor:
    def test_a_qualified_kind_and_key_distinguish_non_slack_conversations(self) -> None:
        anchor = conversation_anchor(kind="mailbox", key="agent/a#question/1")

        assert anchor == f"{SYNTHETIC_LOOP_UMBRELLA_URL}#conversation=mailbox/agent%2Fa%23question%2F1/conversation"
        assert is_conversation_anchor(anchor)
        assert anchor != conversation_anchor(kind="event", key="agent/a#question/1")
        assert anchor != conversation_anchor(kind="mailbox", key="agent/a#question/2")
        assert derive_issue_number(anchor) != "1"

    def test_existing_slack_anchor_stays_recognised(self) -> None:
        anchor = slack_conversation_anchor(channel="D-owner", slack_ts="1.0")

        assert is_conversation_anchor(anchor)
        assert conversation_anchor(kind="slack", key="D-owner/1.0") == anchor

    def test_other_synthetic_fragments_are_not_conversations(self) -> None:
        assert not is_conversation_anchor(f"{SYNTHETIC_LOOP_UMBRELLA_URL}#directive=5")
        assert not is_conversation_anchor(f"{SYNTHETIC_LOOP_UMBRELLA_URL}#conversation=mailbox/")

    @pytest.mark.parametrize(("kind", "key"), [("", "agent/1"), ("mailbox", "")])
    def test_empty_identity_is_refused(self, kind: str, key: str) -> None:
        with pytest.raises(ValueError, match="kind and key must be nonempty"):
            conversation_anchor(kind=kind, key=key)

    @pytest.mark.parametrize("key", ["D-owner", "/1.0", "D-owner/", "D-owner/1.0/extra"])
    def test_slack_identity_requires_channel_and_timestamp(self, key: str) -> None:
        with pytest.raises(ValueError, match="channel/timestamp"):
            conversation_anchor(kind="slack", key=key)
