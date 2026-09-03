"""The 90-char wrap is enforced at the Slack transport, not at a composition seam (#3809).

``normalize_slack_message`` is the documented formatting seam but only four
senders call it; ``daily_digest``, ``speak``, ``slack_answer``, ``self_improve``
and the on-behalf egress all post without it. So the rule is applied one level
lower — in ``SlackBotBackend._post``, the single funnel every in-app
``chat.postMessage`` passes through — and a new sender inherits it by
construction rather than by remembering to call a helper.

``post_routed`` builds its own payload and does NOT delegate to
``post_message``, so :class:`TestTransportWrapsEveryEgress` drives all three
public egress methods: a wrap installed on ``post_message`` alone would leak
every routed post.

``chat.postMessage`` is not the only wire call with a body, so
:class:`TestTheOtherTextBearingEgressesWrap` drives the two that reach Slack
without it: the audio DM's ``initial_comment`` and the incoming webhook.

Only the Slack HTTP boundary (``httpx``) is mocked.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import cast

import httpx
import pytest

from teatree.backends.slack import client as slack_client
from teatree.backends.slack import http as slack_http
from teatree.backends.slack.bot import SlackBotBackend
from teatree.backends.slack.react_errors import SingleEmojiBodyRefusedError
from teatree.slack_mrkdwn import WRAP_WIDTH, slack_line_violations, wrap_slack_message

_SELF_DM = "D_SELF"

_LONG_PROSE = (
    "The nightly dream pass consolidated eleven transcripts into four durable memories "
    "and retired two stale ones, then re-indexed the whole tree so the loader stays "
    "under its line budget on the next session start."
)
_LONG_URL = "https://github.example.com/some-organisation/some-repository/pull/4815162342/files#diff-abcdef0123456789"
_LONG_TOKEN = "src/teatree/core/management/commands/a_very_long_module_name_that_is_never_broken_by_wrap.py"


@dataclass(frozen=True)
class _Call:
    json: dict[str, object]


def _capturing_post(captured: list[_Call]) -> object:
    def fake_post(url: str, **kwargs: object) -> httpx.Response:
        captured.append(_Call(json=cast("dict[str, object]", kwargs["json"])))
        return httpx.Response(200, json={"ok": True, "ts": "1.2"}, request=httpx.Request("POST", url))

    return fake_post


def _capturing_json_post(captured: list[_Call]) -> object:
    """Capture only the JSON-bodied posts; the raw-bytes file upload carries none."""

    def fake_post(url: str, **kwargs: object) -> httpx.Response:
        if isinstance(body := kwargs.get("json"), dict):
            captured.append(_Call(json=cast("dict[str, object]", body)))
        return httpx.Response(200, json={"ok": True, "ts": "1.2"}, request=httpx.Request("POST", url))

    return fake_post


def _upload_url_reserver() -> object:
    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        reserved = {"ok": True, "upload_url": "https://files.example.com/upload/1", "file_id": "F1"}
        return httpx.Response(200, json=reserved, request=httpx.Request("GET", url))

    return fake_get


def _recording(calls: list[str], verb: str) -> object:
    def fake(url: str, **kwargs: object) -> httpx.Response:
        calls.append(f"{verb} {url}")
        return httpx.Response(200, json={"ok": True, "ts": "1.2"}, request=httpx.Request(verb.upper(), url))

    return fake


def _backend() -> SlackBotBackend:
    return SlackBotBackend(bot_token="xoxb-bot", user_token="xoxp-user", user_id="U_ME", dm_channel_id=_SELF_DM)


def _posted_text(captured: list[_Call]) -> str:
    return cast("str", captured[0].json["text"])


class TestTransportWrapsEveryEgress:
    """Every public egress method emits a body with no over-width line."""

    def test_post_message_wraps(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: list[_Call] = []
        monkeypatch.setattr(slack_http.httpx, "post", _capturing_post(captured))
        _backend().post_message(channel="C_TEAM", text=_LONG_PROSE)
        assert slack_line_violations(_posted_text(captured)) == []

    def test_post_reply_wraps(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: list[_Call] = []
        monkeypatch.setattr(slack_http.httpx, "post", _capturing_post(captured))
        _backend().post_reply(channel="C_TEAM", ts="1.0", text=_LONG_PROSE)
        assert slack_line_violations(_posted_text(captured)) == []

    def test_post_routed_wraps(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``post_routed`` bypasses ``post_message`` — a per-method wrap would miss it."""
        captured: list[_Call] = []
        monkeypatch.setattr(slack_http.httpx, "post", _capturing_post(captured))
        _backend().post_routed(channel="C_TEAM", text=_LONG_PROSE)
        assert slack_line_violations(_posted_text(captured)) == []

    def test_wrapped_body_preserves_every_word(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: list[_Call] = []
        monkeypatch.setattr(slack_http.httpx, "post", _capturing_post(captured))
        _backend().post_message(channel="C_TEAM", text=_LONG_PROSE)
        assert _posted_text(captured).split() == _LONG_PROSE.split()


class TestTheOtherTextBearingEgressesWrap:
    """``chat.postMessage`` is not the only wire call that carries a message body.

    The audio DM posts its text as ``files.completeUploadExternal``'s
    ``initial_comment`` and never reaches ``chat.postMessage`` at all, so a
    ``speak.slack``-on DM went out entirely unwrapped. The incoming webhook
    posts raw over ``httpx``, outside the backend transport.
    """

    def test_audio_dm_initial_comment_wraps(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: list[_Call] = []
        monkeypatch.setattr(slack_http.httpx, "get", _upload_url_reserver())
        monkeypatch.setattr(slack_http.httpx, "post", _capturing_json_post(captured))
        audio = tmp_path / "speech.m4a"
        audio.write_bytes(b"audio")
        _backend().post_audio_dm(channel=_SELF_DM, filepath=str(audio), text=_LONG_PROSE)
        assert slack_line_violations(cast("str", captured[-1].json["initial_comment"])) == []

    def test_audio_dm_initial_comment_keeps_every_word(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: list[_Call] = []
        monkeypatch.setattr(slack_http.httpx, "get", _upload_url_reserver())
        monkeypatch.setattr(slack_http.httpx, "post", _capturing_json_post(captured))
        audio = tmp_path / "speech.m4a"
        audio.write_bytes(b"audio")
        _backend().post_audio_dm(channel=_SELF_DM, filepath=str(audio), text=_LONG_PROSE)
        assert cast("str", captured[-1].json["initial_comment"]).split() == _LONG_PROSE.split()

    def test_webhook_message_wraps(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: list[_Call] = []
        monkeypatch.setattr(slack_client.httpx, "post", _capturing_json_post(captured))
        slack_client.post_webhook_message("https://hooks.example.com/T/B/X", _LONG_PROSE)
        assert slack_line_violations(_posted_text(captured)) == []

    def test_webhook_message_keeps_every_word(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: list[_Call] = []
        monkeypatch.setattr(slack_client.httpx, "post", _capturing_json_post(captured))
        slack_client.post_webhook_message("https://hooks.example.com/T/B/X", _LONG_PROSE)
        assert _posted_text(captured).split() == _LONG_PROSE.split()


class TestALiteralStashMarkerNeverBreaksTheTransport:
    """A body carrying the wrapper's own ``NUL<n>NUL`` marker must not raise here.

    ``slack_post_message`` documents an EMPTY error as "nothing came back", so a
    raise from the wrap would break a contract callers read to decide whether a
    re-post is safe.
    """

    _MARKER_BODY = f"{_LONG_PROSE} \x0099\x00"

    def test_post_message_posts_it_as_text(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: list[_Call] = []
        monkeypatch.setattr(slack_http.httpx, "post", _capturing_post(captured))
        _backend().post_message(channel="C_TEAM", text=self._MARKER_BODY)
        assert "\x0099\x00" in _posted_text(captured)


class TestCarveOutsSurviveVerbatim:
    """Breaking these would harm readability — the rule the issue carves out."""

    def test_long_url_is_not_broken(self) -> None:
        assert wrap_slack_message(_LONG_URL) == _LONG_URL

    def test_long_url_is_not_a_violation(self) -> None:
        assert slack_line_violations(_LONG_URL) == []

    def test_long_single_token_is_not_broken(self) -> None:
        assert wrap_slack_message(_LONG_TOKEN) == _LONG_TOKEN

    def test_code_fence_is_preserved(self) -> None:
        fence = f"```\n{'x' * 120}\ncolumn a          column b          column c\n```"
        assert wrap_slack_message(fence) == fence

    def test_code_fence_is_not_a_violation(self) -> None:
        assert slack_line_violations(f"```\n{'x' * 120}\n```") == []

    def test_inline_code_span_is_not_broken(self) -> None:
        text = f"run `{'y' * 100}` now"
        assert f"`{'y' * 100}`" in wrap_slack_message(text)

    def test_table_fence_row_is_preserved(self) -> None:
        row = "| " + " | ".join(f"column-{i:02d}" for i in range(9)) + " |"
        assert wrap_slack_message(row) == row

    def test_mrkdwn_link_is_not_broken_at_its_internal_space(self) -> None:
        link = f"<https://example.com/x|{'a label with spaces ' * 6}>"
        assert link in wrap_slack_message(f"see {link} for detail")

    def test_prose_around_a_long_url_still_wraps(self) -> None:
        assert slack_line_violations(wrap_slack_message(f"{_LONG_PROSE} {_LONG_URL}")) == []


class TestStructurePreserved:
    def test_bullet_continuation_is_indented(self) -> None:
        out = wrap_slack_message(f"- {_LONG_PROSE}")
        first, second = out.split("\n")[:2]
        assert first.startswith("- ")
        assert second.startswith("  ")

    def test_quote_continuation_keeps_the_quote_prefix(self) -> None:
        out = wrap_slack_message(f"> {_LONG_PROSE}")
        assert all(line.startswith("> ") for line in out.split("\n"))

    def test_blank_lines_between_paragraphs_survive(self) -> None:
        assert "\n\n" in wrap_slack_message(f"{_LONG_PROSE}\n\n{_LONG_PROSE}")

    def test_short_text_is_returned_unchanged(self) -> None:
        assert wrap_slack_message("done") == "done"

    def test_empty_text_is_returned_unchanged(self) -> None:
        assert wrap_slack_message("") == ""

    def test_a_marker_with_no_words_is_left_alone(self) -> None:
        """An over-width line whose marker is followed by nothing has no atom to pack."""
        line = "- " + " " * 100
        assert wrap_slack_message(line) == line

    def test_exactly_at_width_is_not_wrapped(self) -> None:
        line = "w " * 44 + "ww"
        assert len(line) == WRAP_WIDTH
        assert wrap_slack_message(line) == line

    def test_one_over_width_is_wrapped(self) -> None:
        line = "w " * 44 + "www"
        assert len(line) == WRAP_WIDTH + 1
        assert "\n" in wrap_slack_message(line)


class TestIdempotent:
    """Re-application at a second seam must be a no-op."""

    @pytest.mark.parametrize(
        "text",
        [
            _LONG_PROSE,
            f"- {_LONG_PROSE}",
            f"> {_LONG_PROSE}",
            f"{_LONG_PROSE}\n\n{_LONG_PROSE}",
            f"{_LONG_PROSE} {_LONG_URL}",
            f"```\n{'x' * 120}\n```",
        ],
    )
    def test_wrapping_twice_equals_wrapping_once(self, text: str) -> None:
        once = wrap_slack_message(text)
        assert wrap_slack_message(once) == once


class TestBlocksUntouched:
    """Block Kit owns its own layout — only ``text`` is wrapped."""

    def test_blocks_payload_is_passed_through_verbatim(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: list[_Call] = []
        monkeypatch.setattr(slack_http.httpx, "post", _capturing_post(captured))
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": _LONG_PROSE}}]
        _backend().post_message(channel="C_TEAM", text=_LONG_PROSE, blocks=blocks)
        assert captured[0].json["blocks"] == blocks


class TestExemptionIsDeliberate:
    """The only escape is a reason string, visible in a diff."""

    def test_a_reason_skips_the_wrap(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: list[_Call] = []
        monkeypatch.setattr(slack_http.httpx, "post", _capturing_post(captured))
        _backend().post_message(channel="C_TEAM", text=_LONG_PROSE, wrap_exempt_reason="pre-wrapped upstream")
        assert _posted_text(captured) == _LONG_PROSE

    def test_the_default_wraps(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: list[_Call] = []
        monkeypatch.setattr(slack_http.httpx, "post", _capturing_post(captured))
        _backend().post_message(channel="C_TEAM", text=_LONG_PROSE)
        assert _posted_text(captured) != _LONG_PROSE


class TestOracleIsAntiVacuous:
    """A green above is only meaningful if the oracle can go red."""

    def test_the_oracle_flags_an_over_width_prose_line(self) -> None:
        assert slack_line_violations(_LONG_PROSE) == [_LONG_PROSE]

    def test_the_oracle_does_not_flag_a_long_url(self) -> None:
        assert slack_line_violations(_LONG_URL) == []

    def test_empty_text_has_no_violations(self) -> None:
        assert slack_line_violations("") == []

    def test_the_oracle_reports_every_offending_line(self) -> None:
        assert len(slack_line_violations(f"{_LONG_PROSE}\n{_LONG_PROSE}")) == 2


class TestGuardOrderSurvivesTheExtraction:
    """The single-emoji refusal must precede token resolution.

    ``_channel_token`` can fire a live ``conversations.info`` Connect probe, so
    a body that is going to be refused must cost no API call at all. Moving the
    publish body into ``egress`` put that ordering at risk — an eagerly
    evaluated token argument would probe first — so it is pinned here.
    """

    def test_single_emoji_is_refused_without_any_api_call(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The Connect probe is a GET, so both verbs must stay untouched."""
        calls: list[str] = []
        monkeypatch.setattr(slack_http.httpx, "post", _recording(calls, "post"))
        monkeypatch.setattr(slack_http.httpx, "get", _recording(calls, "get"))
        with pytest.raises(SingleEmojiBodyRefusedError):
            _backend().post_message(channel="C_TEAM", text=":tada:")
        assert calls == []
