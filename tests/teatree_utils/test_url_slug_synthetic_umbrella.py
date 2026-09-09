"""The synthetic-loop umbrella anchor predicate (souliane/teatree#3706).

Every synthetic loop ticket — directive interpret/implement, outer-loop experiment —
anchors on ONE umbrella issue and disambiguates via a URL fragment. The predicate
recognises that anchor (any fragment, or none) so the artifact-terminal task sweep can
skip these FSM-owned tasks regardless of their phase.
"""

from teatree.core.models.ticket_number import derive_issue_number
from teatree.utils.url_slug import (
    SYNTHETIC_LOOP_UMBRELLA_URL,
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
