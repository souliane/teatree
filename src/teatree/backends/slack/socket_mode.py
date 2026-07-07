"""Socket Mode readiness — the app-level token probe + manifest gap analysis.

Socket Mode is teatree's primary inbound transport (BLUEPRINT § B5): the worker
holds one WebSocket per overlay, opened with an app-level ``xapp-`` token
carrying ``connections:write``. Three things must hold for events to arrive: the
app-level token is valid, the manifest has Socket Mode enabled with the inbound
events subscribed, and the bot carries the scopes those events require.

Slack exposes NO API to mint an app-level token — it is generated once in the
app's Basic Information page — so that single step is the only part of Socket
Mode setup ``t3`` cannot self-provision. Everything else (the socket-mode flag,
event subscriptions, and bot scopes) is auto-fixable through
``apps.manifest.update``. This module is the mockable boundary the doctor check
drives: :func:`manifest_socket_gaps` is pure, and :func:`probe_app_connections`
isolates the one live Slack call so tests never touch the network.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from teatree.types import RawAPIDict

type SlackManifest = dict[str, Any]
type ConnectionOpener = Callable[[str], RawAPIDict]

SOCKET_MODE_APP_SCOPE = "connections:write"

# The inbound events teatree's Socket Mode receiver consumes: DM messages
# (message.im), @-mentions (app_mention), and emoji reactions (reaction_added).
REQUIRED_SOCKET_EVENTS: frozenset[str] = frozenset({"app_mention", "message.im", "reaction_added"})

# The bot scopes those events require to be delivered / readable.
REQUIRED_SOCKET_BOT_SCOPES: frozenset[str] = frozenset({"app_mentions:read", "im:history", "reactions:read"})


@dataclass(frozen=True, slots=True)
class ManifestSocketGaps:
    """What a manifest lacks for Socket Mode — empty on all counts means ready."""

    socket_mode_disabled: bool
    missing_events: frozenset[str]
    missing_bot_scopes: frozenset[str]

    @property
    def ok(self) -> bool:
        return not (self.socket_mode_disabled or self.missing_events or self.missing_bot_scopes)


def manifest_socket_gaps(manifest: SlackManifest) -> ManifestSocketGaps:
    """Compare *manifest* against the Socket Mode requirements (pure)."""
    settings = manifest.get("settings", {})
    events = set(settings.get("event_subscriptions", {}).get("bot_events", []))
    bot_scopes = set(manifest.get("oauth_config", {}).get("scopes", {}).get("bot", []))
    return ManifestSocketGaps(
        socket_mode_disabled=not bool(settings.get("socket_mode_enabled")),
        missing_events=frozenset(REQUIRED_SOCKET_EVENTS - events),
        missing_bot_scopes=frozenset(REQUIRED_SOCKET_BOT_SCOPES - bot_scopes),
    )


@dataclass(frozen=True, slots=True)
class AppTokenProbe:
    """The classified result of an ``apps.connections.open`` probe."""

    ok: bool
    missing_scope: bool
    error: str

    @classmethod
    def valid(cls) -> "AppTokenProbe":
        return cls(ok=True, missing_scope=False, error="")


def open_app_connection(app_token: str) -> RawAPIDict:
    """POST ``apps.connections.open`` with *app_token*; return the raw JSON body.

    The single live Slack call in the Socket Mode readiness path.
    ``apps.connections.open`` succeeds only for an app-level (``xapp-``) token
    carrying ``connections:write`` — so its result is a direct proof of that
    scope. Tests inject a stub via the ``opener`` parameter of
    :func:`probe_app_connections`.
    """
    response = httpx.post(
        "https://slack.com/api/apps.connections.open",
        headers={"Authorization": f"Bearer {app_token}"},
        timeout=30,
    )
    response.raise_for_status()
    return dict(response.json())


def probe_app_connections(app_token: str, *, opener: ConnectionOpener = open_app_connection) -> AppTokenProbe:
    """Classify whether *app_token* is a working ``connections:write`` app token.

    ``apps.connections.open`` returns ``ok=True`` only for a valid app-level
    token carrying ``connections:write`` — the exact capability Socket Mode
    needs. A ``missing_scope`` (needing ``connections:write``) is flagged
    separately so the doctor can name the precise gap; any other error surfaces
    verbatim.
    """
    body = opener(app_token)
    if body.get("ok"):
        return AppTokenProbe.valid()
    error = str(body.get("error", "unknown error"))
    missing_scope = error == "missing_scope" or body.get("needed") == SOCKET_MODE_APP_SCOPE
    return AppTokenProbe(ok=False, missing_scope=missing_scope, error=error)


__all__ = [
    "REQUIRED_SOCKET_BOT_SCOPES",
    "REQUIRED_SOCKET_EVENTS",
    "SOCKET_MODE_APP_SCOPE",
    "AppTokenProbe",
    "ConnectionOpener",
    "ManifestSocketGaps",
    "SlackManifest",
    "manifest_socket_gaps",
    "open_app_connection",
    "probe_app_connections",
]
