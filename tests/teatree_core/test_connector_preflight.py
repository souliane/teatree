"""Loop-start connector preflight gate (refuse-to-continue on down connector).

An overlay that hard-depends on external connectors must HARD-FAIL
(refuse to continue) when one is unreachable, rather than degrade into
silent no-ops. The gate ``raise SystemExit`` with a message naming
WHICH connector is down.
"""

from unittest.mock import patch

import httpx
import pytest
from django.test import TestCase

from teatree.backends.slack import http as slack_http
from teatree.backends.slack.bot import SlackBotBackend
from teatree.core.connector_manifest import ConnectorRequirement, OverlayManifest
from teatree.core.connector_preflight import assert_required_connectors, assert_slack_scope, run_connector_preflight
from teatree.core.mcp_connectivity import McpServerStatus
from teatree.core.models import Worktree
from teatree.core.overlay import OverlayBase, ProvisionStep


def _fake_auth_test_post(header_value: str | None):
    """Return an ``httpx.post`` stub whose ``auth.test`` carries *header_value*.

    The granted scopes are supplied ONLY through the ``X-OAuth-Scopes``
    response header — never a fabricated JSON ``scopes`` field — so the guard
    is exercised against the same surface Slack actually uses in production.
    A ``None`` header value models a response with no scope header at all.
    """
    headers = {} if header_value is None else {"X-OAuth-Scopes": header_value}

    def fake_post(url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(
            200,
            headers=headers,
            json={"ok": True, "user_id": "UBOT", "bot_id": "BBOT"},
            request=httpx.Request("POST", url),
        )

    return fake_post


class TestAssertSlackScope(TestCase):
    def test_passes_when_scope_present_in_header(self) -> None:
        with patch.object(slack_http.httpx, "post", _fake_auth_test_post("chat:write,reactions:write,users:read")):
            assert_slack_scope(SlackBotBackend(bot_token="xoxb-test"), "reactions:write")

    def test_fires_when_scope_missing_from_header(self) -> None:
        with (
            patch.object(slack_http.httpx, "post", _fake_auth_test_post("chat:write,users:read")),
            pytest.raises(RuntimeError) as excinfo,
        ):
            assert_slack_scope(SlackBotBackend(bot_token="xoxb-test"), "reactions:write")
        assert "reactions:write" in str(excinfo.value)

    def test_fires_when_header_absent_entirely(self) -> None:
        with (
            patch.object(slack_http.httpx, "post", _fake_auth_test_post(None)),
            pytest.raises(RuntimeError),
        ):
            assert_slack_scope(SlackBotBackend(bot_token="xoxb-test"), "reactions:write")


class _NoOpOverlay(OverlayBase):
    def get_repos(self) -> list[str]:
        return ["backend"]

    def get_provision_steps(self, worktree: Worktree) -> list[ProvisionStep]:
        _ = worktree
        return []


class _SlackDownOverlay(OverlayBase):
    def get_repos(self) -> list[str]:
        return ["backend"]

    def get_provision_steps(self, worktree: Worktree) -> list[ProvisionStep]:
        _ = worktree
        return []

    def get_connector_preflight(self) -> list:
        def _probe() -> None:
            msg = "Slack auth.test failed: missing_scope"
            raise RuntimeError(msg)

        return [_probe]


class TestOverlayBaseConnectorPreflightDefault(TestCase):
    def test_default_is_empty(self) -> None:
        assert _NoOpOverlay().get_connector_preflight() == []


class TestRunConnectorPreflight(TestCase):
    def test_clean_overlays_return_none(self) -> None:
        with patch(
            "teatree.core.connector_preflight.get_all_overlays",
            return_value={"clean": _NoOpOverlay()},
        ):
            assert run_connector_preflight() is None

    def test_down_connector_raises_systemexit_naming_the_connector(self) -> None:
        with (
            patch(
                "teatree.core.connector_preflight.get_all_overlays",
                return_value={"acme": _SlackDownOverlay()},
            ),
            pytest.raises(SystemExit) as excinfo,
        ):
            run_connector_preflight()

        assert excinfo.value.code != 0
        message = str(excinfo.value)
        assert "acme" in message
        assert "Slack" in message
        assert "missing_scope" in message

    def test_named_overlay_filter_skips_other_overlays(self) -> None:
        with patch(
            "teatree.core.connector_preflight.get_all_overlays",
            return_value={"clean": _NoOpOverlay(), "acme": _SlackDownOverlay()},
        ):
            # Restricting to the clean overlay must not trip the down one.
            assert run_connector_preflight("clean") is None

    def test_named_overlay_filter_still_gates_the_selected_overlay(self) -> None:
        with (
            patch(
                "teatree.core.connector_preflight.get_all_overlays",
                return_value={"clean": _NoOpOverlay(), "acme": _SlackDownOverlay()},
            ),
            pytest.raises(SystemExit) as excinfo,
        ):
            run_connector_preflight("acme")

        assert excinfo.value.code != 0

    def test_unknown_named_overlay_is_a_clean_noop(self) -> None:
        with patch(
            "teatree.core.connector_preflight.get_all_overlays",
            return_value={"acme": _SlackDownOverlay()},
        ):
            assert run_connector_preflight("does-not-exist") is None


class _ManifestOverlay(OverlayBase):
    def get_repos(self) -> list[str]:
        return ["backend"]

    def get_provision_steps(self, worktree: Worktree) -> list[ProvisionStep]:
        _ = worktree
        return []

    def get_connector_manifest(self) -> list[ConnectorRequirement]:
        return [ConnectorRequirement("claude.ai Slack", required=True)]


class TestManifestRequiredConnectorGate(TestCase):
    def test_required_declared_connector_down_refuses_the_loop(self) -> None:
        with (
            patch(
                "teatree.core.connector_preflight.get_all_overlays",
                return_value={"acme": _ManifestOverlay()},
            ),
            patch(
                "teatree.core.connector_manifest.probe_mcp_servers",
                return_value=[McpServerStatus("claude.ai Slack", "", connected=False)],
            ),
            pytest.raises(SystemExit) as excinfo,
        ):
            run_connector_preflight()
        assert "claude.ai Slack" in str(excinfo.value)

    def test_required_declared_connector_connected_passes(self) -> None:
        with (
            patch(
                "teatree.core.connector_preflight.get_all_overlays",
                return_value={"acme": _ManifestOverlay()},
            ),
            patch(
                "teatree.core.connector_manifest.probe_mcp_servers",
                return_value=[McpServerStatus("claude.ai Slack", "", connected=True)],
            ),
        ):
            assert run_connector_preflight() is None


class TestAssertRequiredConnectors(TestCase):
    def test_down_required_connector_raises(self) -> None:
        manifests = [OverlayManifest("ov", [ConnectorRequirement("claude.ai Slack", required=True)])]
        with pytest.raises(RuntimeError, match="not connected"):
            assert_required_connectors(
                manifests,
                probe=lambda: [McpServerStatus("claude.ai Slack", "", connected=False)],
            )

    def test_empty_manifest_is_a_noop_without_probing(self) -> None:
        # No probe callable supplied and none run — the empty manifest short-circuits.
        assert assert_required_connectors([]) is None
