"""Tests for the wave-2 teatree WRITE MCP tools: review-request + slack react (#3076 item 3).

``review_request_check`` / ``review_request_post`` wrap the exact
``review_request_check`` / ``review_request_post`` management commands (the
#1094 dedup + #960 on-behalf + review-state gate chain), and ``slack_react``
routes through :class:`~teatree.core.on_behalf_egress.OnBehalfSlackEgress` — the
single colleague-surface Slack egress owner (send-proxy + on-behalf gate +
notify receipt). Each tool is exercised through ``MCPServer.call_tool`` so the
gates fire identically over MCP.
"""

import sys
from typing import Any
from unittest.mock import patch

import pytest
from asgiref.sync import async_to_sync
from django.test import TestCase
from mcp.server.mcpserver.exceptions import ToolError

from teatree.backends.slack import http as slack_http
from teatree.backends.types import Service
from teatree.core.backend_protocols import DraftState
from teatree.core.gates.review_request_guard import GuardTarget
from teatree.core.modelkit.phase_tools import mcp_write_tools_for_phase
from teatree.core.models import ConfigSetting, ReviewEvidence, Ticket
from teatree.core.overlay import OverlayConfig, OverlayConnectors
from teatree.mcp.server import build_server
from teatree.mcp.write_tool_run import _last_json_object, run_command, run_emitting_command
from tests._send_gate import allow_slack_channels
from tests.teatree_core._on_behalf_gate_helpers import seed_forbidding_posture
from tests.teatree_core.test_review_request_guard import FakeClient
from tests.teatree_mcp._call_tool_result import payloads as _payloads

_CHECK_CMD = "teatree.core.management.commands.review_request_check"
_POST_CMD = "teatree.core.management.commands.review_request_post"
_MR_URL = "https://github.com/acme/widgets/pull/9"
_SHA = "a" * 40


def _call(tool: str, args: dict[str, Any]) -> Any:
    return _payloads(async_to_sync(build_server().call_tool)(tool, args))[0]


class _SlackOverlay:
    def __init__(self) -> None:
        self.config = OverlayConfig(required_third_party_services=frozenset({Service.SLACK}))
        self.connectors = OverlayConnectors()


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


class _OwnerAuthoredHost:
    def get_pr_author(self, *, pr_url: str) -> str:
        _ = pr_url
        return "owner"

    def current_user(self) -> str:
        return "owner"

    def list_my_prs(self, *, author: str) -> list[dict[str, Any]]:
        assert author == "owner"
        return [{"html_url": _MR_URL, "title": "fix(widgets): review flow", "head_pipeline": {"status": "success"}}]

    def fetch_pr_draft_state(self, *, slug: str, pr_id: int) -> DraftState:
        assert (slug, pr_id) == ("acme/widgets", 9)
        return DraftState.NOT_DRAFT


class TestReviewRequestCheckTool(TestCase):
    def test_returns_the_gate_decision(self) -> None:
        # The forge confirms a ready, owner-authored MR. With no review channel,
        # the real guard then reports SUPPRESS through the MCP surface.
        ConfigSetting.objects.set_value("user_identity_aliases", ["owner"])
        host = _OwnerAuthoredHost()
        with (
            patch(f"{_CHECK_CMD}.code_host_from_overlay", return_value=host),
            patch("teatree.core.gates.review_request_batch_gate.code_host_from_overlay", return_value=host),
            patch("teatree.core.backend_factory.code_host_from_overlay", return_value=host),
        ):
            result = _call("review_request_check", {"mr_url": _MR_URL})

        assert result["action"] == "suppress"
        assert result["reason"] == "no_review_channel_or_token"


class TestReviewRequestPostTool(TestCase):
    def setUp(self) -> None:
        super().setUp()
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.REVIEW_REQUESTED)
        ticket.record_anti_vacuity_attestation(_SHA, "ACs checked against diff", [], no_new_tests=True)
        ReviewEvidence.record(
            ticket=ticket,
            kind=ReviewEvidence.Kind.COLD_REVIEW,
            reviewer_identity="reviewer-bob",
            verdict="merge_safe",
            head_sha=_SHA,
        )
        self.gate_args = {"ticket_id": str(ticket.pk), "head_sha": _SHA}

    def test_reaches_the_gated_command_and_returns_its_verdict(self) -> None:
        # No review channel + no messaging backend ⇒ the command's draft
        # fallback finds nothing to send and reports suppress. The point is the
        # tool surfaces the command's machine-legible JSON verdict.
        result = _call(
            "review_request_post",
            {"mr_url": _MR_URL, "approver": "user-1", **self.gate_args},
        )

        assert result["action"] in {"suppress", "draft"}
        assert result["reason"] == "no_review_channel_or_token"

    def test_the_requesting_review_server_reaches_the_gated_command(self) -> None:
        server = build_server(read_only=True, allowed_writes=mcp_write_tools_for_phase("requesting_review"))
        args = {"mr_url": _MR_URL, "approver": "user-1", **self.gate_args}

        result = _payloads(async_to_sync(server.call_tool)("review_request_post", args))[0]

        assert result["reason"] == "no_review_channel_or_token"

    def test_refuses_without_a_recorded_on_behalf_approval(self) -> None:
        # A postable channel + no recorded #960 approval ⇒ the on-behalf gate
        # refuses over MCP exactly as on the CLI.
        target = GuardTarget(channel_id="C123", channel_name="reviews", token="tok")
        ConfigSetting.objects.set_value("user_identity_aliases", ["owner"])
        seed_forbidding_posture()
        host = _OwnerAuthoredHost()
        slack = FakeClient(pages=[{"ok": True, "messages": [], "has_more": False}])
        with (
            patch(f"{_POST_CMD}.code_host_from_overlay", return_value=host),
            patch("teatree.core.gates.review_request_batch_gate.code_host_from_overlay", return_value=host),
            patch("teatree.core.backend_factory.code_host_from_overlay", return_value=host),
            patch("teatree.core.management.commands.review_request_post.resolve_guard_target", return_value=target),
            patch.object(slack_http.httpx, "get", slack.get),
        ):
            result = _call(
                "review_request_post",
                {"mr_url": _MR_URL, "approver": "user-1", **self.gate_args},
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
            patch("teatree.mcp.write_tool_run.call_command", side_effect=_boom),
            pytest.raises(ToolError, match="boom: bad input"),
        ):
            run_emitting_command("review_request_post", "--mr-url", "x")

    def test_run_command_returns_the_command_result(self) -> None:
        with patch("teatree.mcp.write_tool_run.call_command", return_value="done") as call:
            assert run_command("workspace", "teardown", path="/x") == "done"
        call.assert_called_once()

    def test_run_command_converts_a_system_exit_into_a_tool_error(self) -> None:
        def _boom(command: str, *_args: object, stderr: object = None, **_kwargs: object) -> None:
            if stderr is not None:
                stderr.write("refused: not clear")
            raise SystemExit(2)

        # MCPServer only converts Exception (not BaseException) — run_command must
        # re-raise the SystemExit as a ToolError carrying the stderr message.
        with (
            patch("teatree.mcp.write_tool_run.call_command", side_effect=_boom),
            pytest.raises(ToolError, match="refused: not clear"),
        ):
            run_command("ticket", "merge", "7")

    def test_run_command_defaults_the_exit_code_when_none_is_carried(self) -> None:
        # a bare SystemExit carries code=None; run_command falls back to exit_code/1
        with (
            patch("teatree.mcp.write_tool_run.call_command", side_effect=SystemExit()),
            pytest.raises(ToolError, match="exit 1"),
        ):
            run_command("ticket", "merge", "7")


class TestSlackReactTool(TestCase):
    def test_colleague_react_without_approval_is_blocked_by_the_on_behalf_gate(self) -> None:
        allow_slack_channels("C999")
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
