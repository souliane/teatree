"""The hook-process Slack egress wraps at 90 too (#3809).

``hooks/slack_mirror.py`` is the second ``chat.postMessage`` egress: it runs in
the PreToolUse hook process and posts the ``AskUserQuestion`` mirror through an
INJECTED ``Poster``, so it never reaches ``SlackBotBackend._post`` where the
transport wrap lives. Without its own call it would be the one surface that can
still emit an unwrapped message.
"""

from typing import Any

from teatree.hooks.slack_mirror import slack_post_message
from teatree.slack_mrkdwn import slack_line_violations

_LONG_PROSE = (
    "The question mirror posts the pending AskUserQuestion to the owner's DM so it "
    "reaches their phone before the in-client prompt renders, under a tight hook timeout."
)


class _CapturingPoster:
    def __init__(self, *, ok: bool = True) -> None:
        self.bodies: list[dict[str, Any]] = []
        self._ok = ok

    def __call__(self, method: str, *, token: str, json: dict, idempotent: bool) -> dict:
        self.bodies.append(json)
        return {"ok": self._ok, "ts": "1.2"}


class TestMirrorWraps:
    def test_posted_body_has_no_over_width_line(self) -> None:
        poster = _CapturingPoster()
        slack_post_message(poster, "D_SELF", _LONG_PROSE, bot_token="xoxb-bot")
        assert slack_line_violations(poster.bodies[0]["text"]) == []

    def test_every_word_survives_the_wrap(self) -> None:
        poster = _CapturingPoster()
        slack_post_message(poster, "D_SELF", _LONG_PROSE, bot_token="xoxb-bot")
        assert poster.bodies[0]["text"].split() == _LONG_PROSE.split()

    def test_thread_ts_still_threads(self) -> None:
        poster = _CapturingPoster()
        slack_post_message(poster, "D_SELF", _LONG_PROSE, bot_token="xoxb-bot", thread_ts="9.9")
        assert poster.bodies[0]["thread_ts"] == "9.9"

    def test_returns_the_posted_ts(self) -> None:
        poster = _CapturingPoster()
        assert slack_post_message(poster, "D_SELF", "short", bot_token="xoxb-bot") == "1.2"

    def test_not_ok_still_degrades_to_empty(self) -> None:
        poster = _CapturingPoster(ok=False)
        assert slack_post_message(poster, "D_SELF", _LONG_PROSE, bot_token="xoxb-bot") == ""


class TestOracleIsAntiVacuous:
    def test_the_unwrapped_body_would_have_failed_the_assertion(self) -> None:
        assert slack_line_violations(_LONG_PROSE) == [_LONG_PROSE]
