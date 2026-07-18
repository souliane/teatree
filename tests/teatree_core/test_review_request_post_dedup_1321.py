"""End-to-end Surface C verification for #1321.

The ``review_request_post`` CLI must scan the configured review-team
channel's last 24h before posting. A hit returned by
``conversations.history`` for the canonical MR URL refuses the post with
a structured ``{"action": "suppress", "reason": "already_posted", "permalink": ...}``
dict and exit code 0 — no Slack post is sent.

The existing #1084 guard owns the implementation; this test pins the CLI
end-to-end so a future refactor that bypasses the live scan goes RED.
"""

import contextlib
import io
import json
from unittest.mock import patch

import httpx
import pytest
from django.test import TestCase
from django.utils import timezone

from teatree.backends.slack import http as slack_http
from teatree.core.gates.review_request_guard import GuardTarget

_MR_URL = "https://gitlab.com/org/repo/-/merge_requests/385"
_CHANNEL_ID = "C0_REVIEW"
_CHANNEL_NAME = "the-review-team"
_TARGET = GuardTarget(channel_id=_CHANNEL_ID, channel_name=_CHANNEL_NAME, token="xoxp-user")
_CMD_MOD = "teatree.core.management.commands.review_request_post"


class _FakeHttpxClient:
    """Fake for the module-level ``httpx.get`` :class:`SlackHttpClient` calls internally."""

    def __init__(self, *, pages: list[dict[str, object]]) -> None:
        self._pages = pages
        self._idx = 0

    def get(self, url: str, **_kw: object) -> httpx.Response:
        if "auth.test" in url:
            return httpx.Response(
                200,
                json={"ok": True, "url": "https://team.slack.com/"},
                request=httpx.Request("GET", url),
            )
        if "conversations.history" in url:
            page = self._pages[self._idx] if self._idx < len(self._pages) else {"ok": False}
            self._idx += 1
            return httpx.Response(200, json=page, request=httpx.Request("GET", url))
        return httpx.Response(404, request=httpx.Request("GET", url))


class _PostRecordingBackend:
    def __init__(self) -> None:
        self.posts: list[dict[str, str]] = []

    def post_message(self, *, channel: str, text: str, thread_ts: str = "") -> dict[str, object]:
        self.posts.append({"channel": channel, "text": text, "thread_ts": thread_ts})
        return {"ok": True, "ts": "1.0"}

    def get_permalink(self, *, channel: str, ts: str) -> str:
        return f"https://team.slack.com/archives/{channel}/p{ts.replace('.', '')}"


def _run_post() -> tuple[int, dict[str, object]]:
    from django.core.management import call_command  # noqa: PLC0415

    buf = io.StringIO()
    code = 0
    with contextlib.redirect_stdout(buf):
        try:
            call_command(
                "review_request_post",
                "--mr-url",
                _MR_URL,
                "--approver",
                "souliane",
            )
        except SystemExit as exc:
            code = int(exc.code) if isinstance(exc.code, int) else 1
    payload: dict[str, object] = {}
    for raw in buf.getvalue().splitlines():
        line = raw.strip()
        if line.startswith("{"):
            payload = json.loads(line)
    return code, payload


class TestPostRefusedWhenChannelHistoryHasHit(TestCase):
    def test_24h_history_hit_refuses_the_post(self) -> None:
        recent_ts = f"{timezone.now().timestamp():.6f}"
        page = {
            "ok": True,
            "messages": [
                {"text": f"feat(scope): please review {_MR_URL}", "ts": recent_ts, "user": "U_HUMAN"},
            ],
            "has_more": False,
        }
        backend = _PostRecordingBackend()
        fake = _FakeHttpxClient(pages=[page])
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(slack_http.httpx, "get", fake.get)
            with (
                patch(f"{_CMD_MOD}.resolve_guard_target", return_value=_TARGET),
                patch(f"{_CMD_MOD}.messaging_from_overlay", return_value=backend),
            ):
                code, payload = _run_post()

        assert code == 0
        assert payload["action"] == "suppress"
        assert payload["reason"] == "already_posted"
        assert isinstance(payload.get("permalink"), str)
        assert payload["permalink"].startswith("https://team.slack.com/archives/")
        assert backend.posts == [], "live-channel hit MUST prevent the post from going out"
