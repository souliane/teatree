r"""``t3 slack react`` is the only sanctioned reaction surface (#1281).

Two structural invariants land here:

1.  ``reactions.add`` failures raise :class:`SlackReactionError` — never
    silently return False — so callers cannot accidentally fall back to a
    ``chat.postMessage`` thread reply containing the emoji. The
    ``missing_scope`` case is the primary trigger (BINDING memory
    ``feedback_react_not_emoji_thread_comment``) but every Slack-side
    ``ok:false`` is loud.
2.  ``SlackBotBackend.post_message`` / ``post_reply`` reject a body that
    is a single ``:emoji:`` token (``^:[a-z0-9_+\-]+:$``) with
    :class:`SingleEmojiBodyRefusedError`, pointing at ``t3 slack react``.
    A single-emoji body is the failure-mode shape — banning it forecloses
    the silent fallback path that produced thread spam on three colleague
    broadcasts on 2026-05-20.
"""

from unittest.mock import MagicMock, patch

import httpx
import pytest
from typer.testing import CliRunner

from teatree.backends import slack_reactions
from teatree.backends.slack_bot import SlackBotBackend
from teatree.backends.slack_react_errors import SingleEmojiBodyRefusedError, SlackReactionError, is_single_emoji_body
from teatree.cli.slack_listen import post_reaction, slack_app

runner = CliRunner()


def _response(*, status: int = 200, payload: dict[str, object] | None = None) -> MagicMock:
    response = MagicMock(spec=httpx.Response)
    response.is_success = status < 400
    response.status_code = status
    response.json.return_value = payload or {"ok": True}
    return response


class TestPostReactionRaisesOnSlackError:
    """``cli/slack_listen.post_reaction`` raises on every Slack ``ok:false``.

    The pre-#1281 helper returned ``False`` on ``missing_scope`` and any
    other API-level error. A caller could then "fall back" to posting
    ``chat.postMessage(text=":emoji:")`` on the same broadcast's thread.
    The BINDING memory ``feedback_react_not_emoji_thread_comment`` forbids
    that fallback. The structural fix is to make the failure loud at the
    helper boundary so no caller can swallow it.
    """

    def test_missing_scope_raises(self) -> None:
        payload = {"ok": False, "error": "missing_scope"}
        with (
            patch("teatree.cli.slack_listen.httpx.post", return_value=_response(payload=payload)),
            pytest.raises(SlackReactionError) as exc_info,
        ):
            post_reaction(token="xoxp-1", channel="D1", ts="1.0", emoji="eyes")

        assert exc_info.value.error_code == "missing_scope"
        message = str(exc_info.value)
        assert "missing_scope" in message
        # The message MUST steer the operator to the documented remediation —
        # the scope provisioner CLI and #1232 (the scope-gap issue).
        assert "t3 setup slack-user-token" in message
        assert "1232" in message
        # And it MUST reference the BINDING that forbids the thread-emoji
        # fallback, so any reader of the traceback knows why we raise.
        assert "feedback_react_not_emoji_thread_comment" in message

    def test_not_in_channel_raises(self) -> None:
        payload = {"ok": False, "error": "not_in_channel"}
        with (
            patch("teatree.cli.slack_listen.httpx.post", return_value=_response(payload=payload)),
            pytest.raises(SlackReactionError) as exc_info,
        ):
            post_reaction(token="xoxp-1", channel="D1", ts="1.0", emoji="eyes")
        assert exc_info.value.error_code == "not_in_channel"

    def test_connect_restricted_raises(self) -> None:
        payload = {"ok": False, "error": "mcp_externally_shared_channel_restricted"}
        with (
            patch("teatree.cli.slack_listen.httpx.post", return_value=_response(payload=payload)),
            pytest.raises(SlackReactionError) as exc_info,
        ):
            post_reaction(token="xoxp-1", channel="C-shared", ts="1.0", emoji="white_check_mark")
        assert exc_info.value.error_code == "mcp_externally_shared_channel_restricted"

    def test_already_reacted_does_not_raise(self) -> None:
        payload = {"ok": False, "error": "already_reacted"}
        with patch("teatree.cli.slack_listen.httpx.post", return_value=_response(payload=payload)):
            assert post_reaction(token="xoxp-1", channel="D1", ts="1.0", emoji="eyes") is True

    def test_ok_response_does_not_raise(self) -> None:
        with patch("teatree.cli.slack_listen.httpx.post", return_value=_response()):
            assert post_reaction(token="xoxp-1", channel="D1", ts="1.0", emoji="eyes") is True


class TestAddReactionRaisesOnSlackError:
    """``backends.slack_reactions.add_reaction`` raises on every Slack ``ok:false``.

    Same rule as the CLI helper. The FSM-side wrapper
    (``add_reactions_for_transition``) catches the exception and continues
    so a Slack outage cannot block an FSM transition — but the helper
    itself raises so no future caller can silently swallow the error and
    substitute a thread-emoji post.
    """

    def test_missing_scope_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        post = lambda *_a, **_kw: _response(payload={"ok": False, "error": "missing_scope"})  # noqa: E731
        monkeypatch.setattr(slack_reactions.httpx, "post", post)
        with pytest.raises(SlackReactionError) as exc_info:
            slack_reactions.add_reaction("xoxp", "C1", "1.0", "tada")
        assert exc_info.value.error_code == "missing_scope"
        assert "feedback_react_not_emoji_thread_comment" in str(exc_info.value)

    def test_already_reacted_does_not_raise(self, monkeypatch: pytest.MonkeyPatch) -> None:
        post = lambda *_a, **_kw: _response(payload={"ok": False, "error": "already_reacted"})  # noqa: E731
        monkeypatch.setattr(slack_reactions.httpx, "post", post)
        assert slack_reactions.add_reaction("xoxp", "C1", "1.0", "tada") is True

    def test_ok_returns_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(slack_reactions.httpx, "post", lambda *_a, **_kw: _response())
        assert slack_reactions.add_reaction("xoxp", "C1", "1.0", "tada") is True

    def test_fsm_wrapper_swallows_react_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``add_reactions_for_transition`` keeps FSM transitions resilient.

        The helper raises loudly, but the FSM-side wrapper must NOT
        propagate the raise — a Slack outage during a state transition
        must not roll back the transition. The wrapper counts the failed
        reaction as a no-op (0 in the return) and the next tick re-tries.
        """
        from types import SimpleNamespace  # noqa: PLC0415

        class _Cfg:
            def get_slack_token(self) -> str:
                return "xoxp"

            def get_transition_emojis(self) -> dict[str, str]:
                return {"mark_merged": "tada"}

        overlay = SimpleNamespace(config=_Cfg())
        monkeypatch.setattr(slack_reactions, "get_overlay", lambda name=None: overlay)

        def _raise(*_a: object, **_kw: object) -> bool:
            code = "missing_scope"
            msg = f"Slack reactions.add refused: {code}"
            raise SlackReactionError(code, msg)

        monkeypatch.setattr(slack_reactions, "add_reaction", _raise)

        ticket = SimpleNamespace(
            extra={"prs": {"a": {"review_permalink": "https://t.slack.com/archives/C1/p1700000000000100"}}},
            overlay="",
            role="author",
        )
        # No exception propagates; FSM treats it as 0 successful reactions.
        assert slack_reactions.add_reactions_for_transition(ticket, "mark_merged") == 0


class TestReactCommandSurfaceMissingScope:
    """``t3 slack react`` exits 1 on ``missing_scope`` with a structured hint.

    Pre-#1281 the CLI exited 2 with the generic ``reactions.add failed``
    message regardless of the underlying error. The structured hint at
    exit 1 is required so any operator who hits the scope gap is
    immediately pointed at the documented remediation — never at a
    chat.postMessage fallback.
    """

    def test_missing_scope_exits_1_with_pointer(self) -> None:
        payload = {"ok": False, "error": "missing_scope"}
        with (
            patch("teatree.cli.slack_listen._resolve_reaction_token", return_value="xoxp-1"),
            patch("teatree.cli.slack_listen.httpx.post", return_value=_response(payload=payload)),
        ):
            result = runner.invoke(slack_app, ["react", "D1", "1.0", "eyes"])

        assert result.exit_code == 1, result.stdout
        # Surface the error code, the remediation CLI, and #1232.
        assert "missing_scope" in result.stdout
        assert "t3 setup slack-user-token" in result.stdout
        assert "1232" in result.stdout


class TestSlackBotBackendRejectsSingleEmojiBody:
    """``SlackBotBackend.post_message`` / ``post_reply`` refuse ``^:[a-z_]+:$`` bodies.

    The failure-mode shape this guards against: an agent that wanted to
    react but couldn't (missing_scope, restricted channel) substituting a
    ``chat.postMessage(text=":white_check_mark:")``. Banning the shape
    forecloses the substitute and forces the operator back to
    ``reactions.add`` (i.e. ``t3 slack react``).
    """

    def _backend(self) -> SlackBotBackend:
        return SlackBotBackend(bot_token="xoxb-test", user_id="U1")

    def test_post_message_rejects_single_emoji(self) -> None:
        backend = self._backend()
        with pytest.raises(SingleEmojiBodyRefusedError) as exc_info:
            backend.post_message(channel="C1", text=":white_check_mark:")
        msg = str(exc_info.value)
        assert "t3 slack react" in msg
        assert ":white_check_mark:" in msg

    def test_post_reply_rejects_single_emoji(self) -> None:
        backend = self._backend()
        with pytest.raises(SingleEmojiBodyRefusedError):
            backend.post_reply(channel="C1", ts="1700000000.000100", text=":eyes:")

    def test_post_reply_rejects_single_emoji_with_whitespace(self) -> None:
        """Leading/trailing whitespace must not bypass the gate."""
        backend = self._backend()
        with pytest.raises(SingleEmojiBodyRefusedError):
            backend.post_reply(channel="C1", ts="1700000000.000100", text="  :tada:  ")

    def test_post_message_allows_normal_text(self) -> None:
        backend = self._backend()
        with patch.object(backend, "_post", return_value={"ok": True}) as posted:
            backend.post_message(channel="C1", text="Approved, posted :white_check_mark:.")
        # Normal body passes through to Slack.
        posted.assert_called_once()

    def test_post_message_allows_empty_text(self) -> None:
        """An empty body is not a single-emoji body; the guard does not fire here."""
        backend = self._backend()
        with patch.object(backend, "_post", return_value={"ok": True}):
            backend.post_message(channel="C1", text="")


class TestIsSingleEmojiBody:
    """The pure predicate behind the single-emoji guard."""

    @pytest.mark.parametrize(
        "body",
        [
            ":white_check_mark:",
            ":eyes:",
            ":tada:",
            "  :white_check_mark:  ",
            "\t:eyes:\n",
            ":thumbsup:",
            ":+1:",
            ":-1:",
        ],
    )
    def test_matches_single_emoji(self, body: str) -> None:
        assert is_single_emoji_body(body) is True

    @pytest.mark.parametrize(
        "body",
        [
            "",
            "Approved.",
            "Approved :white_check_mark:.",
            ":white_check_mark: :tada:",
            "  hello  ",
            ":not closed",
            "not opened:",
        ],
    )
    def test_does_not_match_normal_text(self, body: str) -> None:
        assert is_single_emoji_body(body) is False
