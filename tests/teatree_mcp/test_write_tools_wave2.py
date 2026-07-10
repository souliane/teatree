"""Tests for the wave-2 teatree WRITE MCP tools: review-request + slack react (#3076 item 3).

``review_request_check`` / ``review_request_post`` wrap the exact
``review_request_check`` / ``review_request_post`` management commands (the
#1094 dedup + #960 on-behalf + review-state gate chain), and ``slack_react``
routes through :class:`~teatree.core.on_behalf_egress.OnBehalfSlackEgress` — the
single colleague-surface Slack egress owner (send-proxy + on-behalf gate +
notify receipt). Each tool is exercised through ``FastMCP.call_tool`` so the
gates fire identically over MCP.
"""

import json
import sys
from typing import Any
from unittest.mock import patch

import pytest
from asgiref.sync import async_to_sync
from django.test import TestCase

from teatree.backends.types import Service
from teatree.core.gates.review_request_guard import GuardTarget
from teatree.core.overlay import OverlayConfig
from teatree.mcp import build_server
from teatree.mcp.write_tools import _last_json_object, _run_emitting_command


def _payloads(result: Any) -> list[Any]:
    blocks = result[0] if isinstance(result, tuple) else result
    return [json.loads(block.text) for block in blocks if getattr(block, "text", None) is not None]


def _call(tool: str, args: dict[str, Any]) -> Any:
    return _payloads(async_to_sync(build_server().call_tool)(tool, args))[0]


class _SlackOverlay:
    def __init__(self) -> None:
        self.config = OverlayConfig(required_third_party_services=frozenset({Service.SLACK}))


class _FakeMessaging:
    """Enough of a messaging backend to drive OnBehalfSlackEgress hermetically."""

    def __init__(self, *, is_self: bool) -> None:
        self.route_token = "xoxp"  # non-None so the #1750 classifier is consulted
        self._is_self = is_self
        self.reacted: list[dict[str, str]] = []

    def _is_self_dm(self, channel: str) -> bool:
        _ = channel
        return self._is_self

    def react_routed(self, *, channel: str, ts: str, emoji: str) -> dict[str, Any]:
        self.reacted.append({"channel": channel, "ts": ts, "emoji": emoji})
        return {"ok": True, "channel": channel, "ts": ts}


class TestReviewRequestCheckTool(TestCase):
    def test_returns_the_gate_decision(self) -> None:
        # Bare test env has no review channel configured, so the guard peeks
        # SUPPRESS — proving the tool reaches the real review-request guard.
        result = _call("review_request_check", {"mr_url": "https://github.com/acme/widgets/pull/9"})

        assert result["action"] == "suppress"
        assert result["reason"] == "no_review_channel_or_token"


class TestReviewRequestPostTool(TestCase):
    def test_reaches_the_gated_command_and_returns_its_verdict(self) -> None:
        # No review channel + no messaging backend ⇒ the command's draft
        # fallback finds nothing to send and reports suppress. The point is the
        # tool surfaces the command's machine-legible JSON verdict.
        result = _call(
            "review_request_post",
            {"mr_url": "https://github.com/acme/widgets/pull/9", "approver": "user-1"},
        )

        assert result["action"] in {"suppress", "draft"}
        assert result["reason"] == "no_review_channel_or_token"

    def test_refuses_without_a_recorded_on_behalf_approval(self) -> None:
        # A postable channel + no recorded #960 approval ⇒ the on-behalf gate
        # refuses over MCP exactly as on the CLI.
        target = GuardTarget(channel_id="C123", channel_name="reviews", token="tok")
        with (
            patch("teatree.core.management.commands.review_request_post.resolve_guard_target", return_value=target),
            patch(
                "teatree.core.management.commands.review_request_post.should_post_review_request",
            ) as should_post,
            patch(
                "teatree.core.management.commands.review_request_post.on_behalf_block_message",
                return_value="blocked: record an approval with `t3 review approve-on-behalf`",
            ),
        ):
            should_post.return_value.should_post = True
            should_post.return_value.reason = ""
            should_post.return_value.permalink = ""
            result = _call(
                "review_request_post",
                {"mr_url": "https://github.com/acme/widgets/pull/9", "approver": "user-1"},
            )

        assert result["action"] == "refused"
        assert result["reason"] == "on_behalf_not_approved"


class TestJsonEmittingCommandHelpers(TestCase):
    def test_last_json_object_skips_noise_and_returns_the_last_object(self) -> None:
        # Reversed scan hits, in order: an invalid-JSON braces line (suppressed),
        # an unclosed-brace line (not a braces pair), a prose line, then the real
        # verdict object.
        text = '{"action": "post"}\ntrailing prose\n{unclosed\n{bad json}'
        assert _last_json_object(text) == {"action": "post"}

    def test_last_json_object_returns_none_without_a_json_object(self) -> None:
        assert _last_json_object("just prose\nmore prose") is None

    def test_run_emitting_command_surfaces_stderr_when_no_json(self) -> None:
        def _boom(_command: str, *_args: object, **_kwargs: object) -> None:
            sys.stderr.write("boom: bad input")
            raise SystemExit(2)

        with (
            patch("teatree.mcp.write_tools.call_command", side_effect=_boom),
            pytest.raises(RuntimeError, match="boom: bad input"),
        ):
            _run_emitting_command("review_request_post", "--mr-url", "x")


class TestSlackReactTool(TestCase):
    def test_colleague_react_without_approval_is_blocked_by_the_on_behalf_gate(self) -> None:
        fake = _FakeMessaging(is_self=False)
        with (
            patch("teatree.mcp.services_slack._client", return_value=fake),
            patch("teatree.mcp.server.get_all_overlays", return_value={"a": _SlackOverlay()}),
        ):
            result = _call("slack_react", {"channel": "C999", "ts": "1.1", "emoji": ":eyes:"})

        assert result["ok"] is False
        assert "approve-on-behalf" in result["blocked"]
        assert fake.reacted == []

    def test_self_dm_react_bypasses_the_gate_and_reacts(self) -> None:
        fake = _FakeMessaging(is_self=True)
        with (
            patch("teatree.mcp.services_slack._client", return_value=fake),
            patch("teatree.mcp.server.get_all_overlays", return_value={"a": _SlackOverlay()}),
        ):
            result = _call("slack_react", {"channel": "D123", "ts": "1.1", "emoji": "eyes"})

        assert result["ok"] is True
        assert fake.reacted == [{"channel": "D123", "ts": "1.1", "emoji": "eyes"}]
