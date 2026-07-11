"""Socket Mode readiness boundary — manifest gap analysis + app-token probe (#106/B5)."""

from unittest.mock import MagicMock, patch

from teatree.backends.slack.socket_mode import (
    REQUIRED_SOCKET_BOT_SCOPES,
    REQUIRED_SOCKET_EVENTS,
    SOCKET_MODE_APP_SCOPE,
    AppTokenProbe,
    manifest_socket_gaps,
    open_app_connection,
    probe_app_connections,
)
from teatree.cli.slack.manifest import build_manifest


class TestManifestSocketGaps:
    def test_built_manifest_satisfies_socket_mode(self) -> None:
        gaps = manifest_socket_gaps(build_manifest(overlay_name="acme"))
        assert gaps.ok
        assert gaps.socket_mode_disabled is False
        assert gaps.missing_events == frozenset()
        assert gaps.missing_bot_scopes == frozenset()

    def test_reports_missing_reaction_added_event(self) -> None:
        manifest = {
            "settings": {
                "socket_mode_enabled": True,
                "event_subscriptions": {"bot_events": ["app_mention", "message.im"]},
            },
            "oauth_config": {"scopes": {"bot": sorted(REQUIRED_SOCKET_BOT_SCOPES)}},
        }
        gaps = manifest_socket_gaps(manifest)
        assert gaps.missing_events == frozenset({"reaction_added"})
        assert not gaps.ok

    def test_reports_socket_mode_disabled(self) -> None:
        manifest = {
            "settings": {
                "socket_mode_enabled": False,
                "event_subscriptions": {"bot_events": sorted(REQUIRED_SOCKET_EVENTS)},
            },
            "oauth_config": {"scopes": {"bot": sorted(REQUIRED_SOCKET_BOT_SCOPES)}},
        }
        assert manifest_socket_gaps(manifest).socket_mode_disabled is True

    def test_reports_missing_bot_scopes(self) -> None:
        manifest = {
            "settings": {
                "socket_mode_enabled": True,
                "event_subscriptions": {"bot_events": sorted(REQUIRED_SOCKET_EVENTS)},
            },
            "oauth_config": {"scopes": {"bot": ["chat:write"]}},
        }
        assert manifest_socket_gaps(manifest).missing_bot_scopes == REQUIRED_SOCKET_BOT_SCOPES

    def test_empty_manifest_reports_everything_missing(self) -> None:
        gaps = manifest_socket_gaps({})
        assert gaps.socket_mode_disabled is True
        assert gaps.missing_events == REQUIRED_SOCKET_EVENTS
        assert gaps.missing_bot_scopes == REQUIRED_SOCKET_BOT_SCOPES


class TestProbeAppConnections:
    def test_ok_when_connection_opens(self) -> None:
        probe = probe_app_connections("xapp-1", opener=lambda _t: {"ok": True, "url": "wss://x"})
        assert probe == AppTokenProbe.valid()

    def test_missing_scope_flagged(self) -> None:
        probe = probe_app_connections(
            "xapp-1",
            opener=lambda _t: {"ok": False, "error": "missing_scope", "needed": SOCKET_MODE_APP_SCOPE},
        )
        assert probe.ok is False
        assert probe.missing_scope is True

    def test_other_error_surfaced_verbatim(self) -> None:
        probe = probe_app_connections("xapp-1", opener=lambda _t: {"ok": False, "error": "invalid_auth"})
        assert probe.ok is False
        assert probe.missing_scope is False
        assert probe.error == "invalid_auth"


class TestOpenAppConnection:
    def test_posts_to_apps_connections_open_and_returns_body(self) -> None:
        response = MagicMock()
        response.json.return_value = {"ok": True, "url": "wss://x"}
        with patch("teatree.backends.slack.socket_mode.httpx.post", return_value=response) as post:
            body = open_app_connection("xapp-1")
        assert body == {"ok": True, "url": "wss://x"}
        response.raise_for_status.assert_called_once()
        assert post.call_args.args[0] == "https://slack.com/api/apps.connections.open"
        assert post.call_args.kwargs["headers"]["Authorization"] == "Bearer xapp-1"
