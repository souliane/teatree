"""Voice/token mismatch gate for outbound Slack posts (#1395).

A pre-publish classifier between ``chat.postMessage`` and the Slack API
that refuses when the body's *voice* and the *token kind* it
would go out under disagree. The structural fix for a recurrence the
prose-level rule failed to stop: sub-agents posting agent-voice DMs via
the user's personal ``xoxp-`` token, rendering as user-to-self with no
notification.

Two heuristic predicates and one assertion live in
:mod:`teatree.backends.slack.voice_classifier`:

*   :func:`classify_voice` reads message body and returns
    :class:`Voice.AGENT` (status-report markers — PR merged, MR ready,
    evidence, draft note, pipeline, task completed, on-behalf),
    :class:`Voice.USER` (user-voice markers — please review, I'd
    appreciate, RR for, on behalf of), or :class:`Voice.AMBIGUOUS`
    (neither set fires, or both fire — let the post through).
*   :func:`classify_token` inspects the token prefix
    (``xoxp-`` → user, ``xoxb-`` → bot).
*   :func:`assert_voice_token_match` raises
    :class:`SlackVoiceMismatchError` when an agent-voice body would post
    via the personal user token to a user-DM channel (the recurrence
    that motivated the gate), or when a user-voice body would post via
    the bot token..
"""

import pytest

from teatree.backends.slack.voice_classifier import (
    SlackVoiceMismatchError,
    TokenKind,
    Voice,
    assert_voice_token_match,
    classify_token,
    classify_voice,
)


class TestClassifyVoice:
    """The pure body→voice predicate."""

    @pytest.mark.parametrize(
        "body",
        [
            "PR merged: https://github.com/x/y/pull/42",
            "MR ready for review at https://gitlab.com/x/y/-/merge_requests/3",
            "Task completed. Evidence: https://example/permalink",
            "draft note posted on the MR",
            "the agent has shipped feature X",
            "pipeline green on 1234abc",
            "on-behalf approval recorded for action=approve",
            "DM permalink: https://workspace.slack.com/archives/D1/p1700",
        ],
    )
    def test_agent_voice_markers(self, body: str) -> None:
        assert classify_voice(body) is Voice.AGENT

    @pytest.mark.parametrize(
        "body",
        [
            "please review !1234 when you have a moment",
            "I'd appreciate a look at the latest push",
            "RR for !6264",
            "On behalf of the brokerage team, please take a look",
        ],
    )
    def test_user_voice_markers(self, body: str) -> None:
        assert classify_voice(body) is Voice.USER

    @pytest.mark.parametrize(
        "body",
        [
            "",
            "hi",
            "thanks",
            "good morning",
        ],
    )
    def test_ambiguous_neither_marker_set(self, body: str) -> None:
        assert classify_voice(body) is Voice.AMBIGUOUS

    def test_ambiguous_mixed_markers(self) -> None:
        """Both sets fire — agent describing a user request."""
        body = "the agent received: please review !1234"
        assert classify_voice(body) is Voice.AMBIGUOUS


class TestClassifyToken:
    """The pure token→kind predicate."""

    def test_user_token_prefix(self) -> None:
        assert classify_token("xoxp-abc-123") is TokenKind.USER

    def test_bot_token_prefix(self) -> None:
        assert classify_token("xoxb-abc-123") is TokenKind.BOT

    def test_unknown_prefix(self) -> None:
        assert classify_token("xoxa-foo") is TokenKind.UNKNOWN
        assert classify_token("") is TokenKind.UNKNOWN
        assert classify_token("Bearer abc") is TokenKind.UNKNOWN


class TestAssertVoiceTokenMatch:
    """The gate refuses voice/token mismatches."""

    def _dm_channels(self) -> set[str]:
        return {"D0DEMOCLNT1", "D0DEMOTEAM1"}

    def test_agent_voice_personal_token_to_user_dm_refused(self) -> None:
        """The recurrence that motivated the gate (#1395)."""
        with pytest.raises(SlackVoiceMismatchError) as exc_info:
            assert_voice_token_match(
                text="PR merged, evidence at https://example/p1",
                channel="D0DEMOCLNT1",
                token="xoxp-personal",
                dm_channel_ids=self._dm_channels(),
            )
        message = str(exc_info.value)
        assert "agent" in message.lower()
        assert "xoxp" in message or "personal" in message.lower()
        assert "bot" in message.lower()

    def test_agent_voice_bot_token_to_user_dm_allowed(self) -> None:
        """The correct routing for an agent-to-user DM."""
        assert_voice_token_match(
            text="PR merged, evidence at https://example/p1",
            channel="D0DEMOCLNT1",
            token="xoxb-bot",
            dm_channel_ids=self._dm_channels(),
        )

    def test_user_voice_personal_token_to_public_channel_allowed(self) -> None:
        """A review-request from the user's voice over the user's xoxp."""
        assert_voice_token_match(
            text="please review !6264 when you have time",
            channel="C-the-review-team",
            token="xoxp-personal",
            dm_channel_ids=self._dm_channels(),
        )

    def test_user_voice_bot_token_to_public_channel_refused(self) -> None:
        """A user-voice review-request must NOT go out under the bot."""
        with pytest.raises(SlackVoiceMismatchError) as exc_info:
            assert_voice_token_match(
                text="please review !6264",
                channel="C-the-review-team",
                token="xoxb-bot",
                dm_channel_ids=self._dm_channels(),
            )
        message = str(exc_info.value)
        assert "user" in message.lower()
        assert "xoxb" in message or "bot" in message.lower()

    def test_ambiguous_voice_never_refused(self) -> None:
        """Mixed-voice / no-marker bodies never trip the gate.

        The classifier only refuses on a *confident* voice/token
        mismatch; an ambiguous body could be either correct or wrong
        and the gate must not block on uncertainty.
        """
        assert_voice_token_match(
            text="the agent received: please review !1234",
            channel="D0DEMOCLNT1",
            token="xoxp-personal",
            dm_channel_ids=self._dm_channels(),
        )
        assert_voice_token_match(
            text="hi",
            channel="D0DEMOCLNT1",
            token="xoxp-personal",
            dm_channel_ids=self._dm_channels(),
        )

    def test_agent_voice_personal_token_to_public_channel_allowed(self) -> None:
        """The gate only refuses agent-voice→personal on user-DM channels.

        An agent-voice post to a public channel via the personal token
        is a different failure class (workspace-scoping, not the
        notification-routing bug #1395 guards) and is out of scope of
        the DM-routing gate.
        """
        assert_voice_token_match(
            text="PR merged, evidence at https://example/p1",
            channel="C-public",
            token="xoxp-personal",
            dm_channel_ids=self._dm_channels(),
        )

    def test_unknown_token_never_refused(self) -> None:
        """An unrecognised prefix bypasses the gate.

        Unit tests, dry-runs, and noop backends pass ``""`` or a stub
        string; the gate must not break those paths.
        """
        assert_voice_token_match(
            text="PR merged, evidence at https://example/p1",
            channel="D0DEMOCLNT1",
            token="",
            dm_channel_ids=self._dm_channels(),
        )


class TestSlackBotBackendVoiceClassifier:
    """``SlackBotBackend.post_message`` consults the voice classifier (#1395).

    The default refuses an agent-voice DM via the personal token.
    """

    def test_default_post_message_refused_on_agent_voice_to_user_dm(self) -> None:
        from teatree.backends.slack.bot import SlackBotBackend  # noqa: PLC0415

        backend = SlackBotBackend(
            bot_token="",
            user_token="xoxp-personal",
            user_id="U1",
            dm_channel_id="D0DEMOCLNT1",
        )
        with pytest.raises(SlackVoiceMismatchError):
            backend.post_message(channel="D0DEMOCLNT1", text="PR merged, evidence at https://example/p1")

    def test_post_reply_refuses_agent_voice_to_user_dm(self) -> None:
        from teatree.backends.slack.bot import SlackBotBackend  # noqa: PLC0415

        backend = SlackBotBackend(
            bot_token="",
            user_token="xoxp-personal",
            user_id="U1",
            dm_channel_id="D0DEMOCLNT1",
        )
        with pytest.raises(SlackVoiceMismatchError):
            backend.post_reply(channel="D0DEMOCLNT1", ts="1.0", text="task completed, evidence attached")
