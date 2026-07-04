"""Per-overlay connector manifest + reconnect guidance (PR-19 items 1, 4, 5).

Every scenario injects the manifest + probe (no ``~/.claude.json``, no
``claude mcp list`` subprocess) so the check's mode classification, required-vs-
optional verdict, ``RECONNECT`` line rendering, and the graceful-degradation
seam are asserted against controlled ground truth.
"""

import pytest

from teatree.core.connector_manifest import (
    ConnectorManifestOutcome,
    ConnectorRequirement,
    ConnectorUnavailableError,
    DownConnector,
    OverlayManifest,
    check_connector_manifest,
    require_connector,
)
from teatree.core.mcp_connectivity import McpServerStatus
from teatree.core.models import Worktree
from teatree.core.overlay import OverlayBase, ProvisionStep


def _boom() -> list[McpServerStatus]:
    msg = "claude not on PATH"
    raise FileNotFoundError(msg)


def _status(name: str, *, connected: bool) -> McpServerStatus:
    return McpServerStatus(name=name, url="", connected=connected)


def _manifest(overlay: str, *reqs: ConnectorRequirement) -> list[OverlayManifest]:
    return [OverlayManifest(overlay=overlay, requirements=list(reqs))]


class TestCheckConnectorManifest:
    def test_empty_manifest_passes(self) -> None:
        out = check_connector_manifest(manifests=[], probe=list)
        assert out.ok
        assert out.down == []

    def test_all_connected_passes(self) -> None:
        manifests = _manifest("ov", ConnectorRequirement("claude.ai Slack"))
        out = check_connector_manifest(
            manifests=manifests,
            probe=lambda: [_status("claude.ai Slack", connected=True)],
            ever_connected={"claude.ai Slack"},
        )
        assert out.ok
        assert out.required_findings == []

    def test_required_down_never_connected_is_first_install(self) -> None:
        manifests = _manifest("ov", ConnectorRequirement("claude.ai Notion", required=True))
        out = check_connector_manifest(
            manifests=manifests,
            probe=lambda: [_status("claude.ai Notion", connected=False)],
            ever_connected=set(),
        )
        assert not out.ok
        assert "never connected" in out.required_findings[0]
        assert "Settings → Connectors" in out.required_findings[0]

    def test_required_down_previously_connected_is_reconnect(self) -> None:
        manifests = _manifest("ov", ConnectorRequirement("claude.ai Slack", required=True))
        out = check_connector_manifest(
            manifests=manifests,
            probe=lambda: [_status("claude.ai Slack", connected=False)],
            ever_connected={"claude.ai Slack"},
        )
        assert not out.ok
        assert "reconnect it" in out.required_findings[0]

    def test_optional_down_warns_but_does_not_fail(self) -> None:
        manifests = _manifest("ov", ConnectorRequirement("claude.ai Sentry", required=False))
        out = check_connector_manifest(
            manifests=manifests,
            probe=lambda: [_status("claude.ai Sentry", connected=False)],
            ever_connected=set(),
        )
        assert out.ok  # optional down does not fail the check
        assert out.optional_findings
        assert "optional connector" in out.optional_findings[0]
        assert out.required_findings == []

    def test_probe_failure_degrades_to_warn(self) -> None:
        manifests = _manifest("ov", ConnectorRequirement("claude.ai Slack"))
        out = check_connector_manifest(manifests=manifests, probe=_boom)
        assert out.ok
        assert out.degraded
        assert out.probe_findings
        assert "Could not live-probe" in out.probe_findings[0]

    def test_reconnect_lines_required_first_then_optional(self) -> None:
        manifests = _manifest(
            "ov",
            ConnectorRequirement("claude.ai Optional", required=False),
            ConnectorRequirement("claude.ai Required", required=True),
        )
        out = check_connector_manifest(
            manifests=manifests,
            probe=lambda: [
                _status("claude.ai Optional", connected=False),
                _status("claude.ai Required", connected=False),
            ],
            ever_connected=set(),
        )
        lines = out.reconnect_lines()
        assert lines == [
            "RECONNECT claude.ai Required -> https://claude.ai/settings/connectors",
            "RECONNECT claude.ai Optional -> https://claude.ai/settings/connectors",
        ]

    def test_instruction_overrides_the_reconnect_target(self) -> None:
        req = ConnectorRequirement("claude-in-chrome", instruction="reconnect from the extension popup")
        out = check_connector_manifest(
            manifests=_manifest("ov", req),
            probe=lambda: [_status("claude-in-chrome", connected=False)],
            ever_connected=set(),
        )
        assert out.reconnect_lines() == ["RECONNECT claude-in-chrome -> reconnect from the extension popup"]


class TestRequireConnector:
    def test_connected_connector_does_not_raise(self) -> None:
        require_connector("claude.ai Slack", probe=lambda: [_status("claude.ai Slack", connected=True)])

    def test_absent_connector_raises_one_actionable_error(self) -> None:
        with pytest.raises(ConnectorUnavailableError) as excinfo:
            require_connector("claude.ai Slack", probe=lambda: [_status("claude.ai Slack", connected=False)])
        message = str(excinfo.value)
        assert "t3 doctor check" in message
        assert "t3 mcp reconnect" in message

    def test_unprobeable_connector_fails_open(self) -> None:
        # Cannot prove down → do not block the feature.
        require_connector("claude.ai Slack", probe=_boom)


class TestOverlayHook:
    def test_default_is_empty_and_override_declares_connectors(self) -> None:
        class _DefaultOverlay(OverlayBase):
            def get_repos(self) -> list[str]:
                return ["backend"]

            def get_provision_steps(self, worktree: Worktree) -> list[ProvisionStep]:
                _ = worktree
                return []

        class _DeclaringOverlay(_DefaultOverlay):
            def get_connector_manifest(self) -> list[ConnectorRequirement]:
                return [ConnectorRequirement("claude.ai Slack")]

        assert _DefaultOverlay().get_connector_manifest() == []  # default is empty
        assert _DeclaringOverlay().get_connector_manifest() == [ConnectorRequirement("claude.ai Slack")]


class TestOutcomeShape:
    def test_down_connector_carries_its_failure_mode(self) -> None:
        outcome = ConnectorManifestOutcome(
            ok=False,
            down=[DownConnector(requirement=ConnectorRequirement("x"), overlay="ov", ever_connected=True)],
        )
        assert outcome.down[0].ever_connected is True
        assert outcome.reconnect_lines() == ["RECONNECT x -> https://claude.ai/settings/connectors"]
