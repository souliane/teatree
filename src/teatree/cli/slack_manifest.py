"""Slack app-manifest helpers — build, compare, and call the Slack manifest API.

Extracted from :mod:`teatree.cli.slack_setup` to keep that module under the
LOC ceiling.  All public symbols remain importable from ``slack_setup`` via
explicit re-export for backward compatibility.
"""

import json
import urllib.parse
from typing import Any

import httpx

type SlackManifest = dict[str, Any]

_CONFIG_TOKEN_REF = "teatree/slack-app-config-token"  # noqa: S105 — pass key name, not a secret
_CONFIG_REFRESH_REF = "teatree/slack-app-config-refresh"


class SlackManifestError(RuntimeError):
    """Slack ``apps.manifest.*`` (or ``tooling.tokens.rotate``) returned ok=False."""


_BOT_SCOPES = [
    "app_mentions:read",
    "channels:history",
    "channels:read",
    "chat:write",
    "files:write",
    "groups:history",
    "groups:read",
    "im:history",
    "im:read",
    "im:write",
    "mpim:history",
    "mpim:read",
    "reactions:read",
    "reactions:write",
    "users:read",
    "users:read.email",
]
_BOT_ONLY_SCOPES = frozenset(
    {
        "chat:write.customize",
        "chat:write.public",
    }
)
# Scopes for the human user's OAuth token (``xoxp-…``). ``SlackBotBackend``
# routes every outbound call (``chat.postMessage``, ``reactions.add`` /
# ``reactions.get``) through this token for Slack-Connect externally-shared
# channels — and for any channel whose Connect membership cannot be
# confirmed, where writes/reactions fail toward the user xoxp while reads
# fail safe to the bot (see ``SlackBotBackend._channel_token``, #1110) —
# because those channels reject the bot token with
# ``mcp_externally_shared_channel_restricted`` — hence ``chat:write``
# (posting) plus ``reactions:read`` / ``reactions:write``.
# ``build_manifest`` must declare a ``user`` scopes section or a reinstall
# never re-prompts for these grants and the xoxp token keeps whatever Slack
# defaulted (empirically: no reaction scopes).
# A manifest reinstall re-prompts OAuth consent for *exactly* this set and
# drops any user scope not listed, so the set must be a SUPERSET that keeps
# the capability the xoxp token is already relied on for: ``chat:write``
# (posting into Slack-Connect channels under the user's identity) and
# ``users:read`` (handle/id resolution). Listing only the two reaction
# scopes would silently revoke those on reinstall.
_USER_SCOPES = [
    "canvases:read",
    "canvases:write",
    "channels:history",
    "chat:write",
    "files:read",
    "groups:history",
    "im:history",
    "mpim:history",
    "reactions:read",
    "reactions:write",
    "search:read.files",
    "search:read.im",
    "search:read.mpim",
    "search:read.private",
    "search:read.public",
    "search:read.users",
    "users:read",
    "users:read.email",
]
_BOT_EVENTS = ["app_mention", "message.im"]


def _user_scopes_carry_no_bot_only_scope() -> None:
    leaked = _BOT_ONLY_SCOPES.intersection(_USER_SCOPES)
    if leaked:
        joined = ", ".join(sorted(leaked))
        message = f"_USER_SCOPES contains bot-only scope(s) Slack rejects on a user token: {joined}"
        raise AssertionError(message)


_user_scopes_carry_no_bot_only_scope()


def build_manifest(*, overlay_name: str, display_name: str = "") -> SlackManifest:
    """Build the Slack app manifest payload for *overlay_name*.

    The returned dict matches Slack's app-manifest schema. Display name
    defaults to ``teatree-<overlay>`` when not overridden.
    """
    name = display_name or f"teatree-{overlay_name}"
    return {
        "display_information": {
            "name": name,
            "description": f"Teatree agent bot for the {overlay_name} overlay.",
        },
        "features": {
            "app_home": {
                "home_tab_enabled": False,
                "messages_tab_enabled": True,
                "messages_tab_read_only_enabled": False,
            },
            "bot_user": {"display_name": name, "always_online": True},
        },
        "oauth_config": {"scopes": {"bot": _BOT_SCOPES, "user": _USER_SCOPES}},
        "settings": {
            "event_subscriptions": {"bot_events": _BOT_EVENTS},
            "interactivity": {"is_enabled": False},
            "org_deploy_enabled": False,
            "socket_mode_enabled": True,
            "token_rotation_enabled": False,
        },
    }


_SLACK_CREATE_APP_URL = "https://api.slack.com/apps?new_app=1&manifest_json="


def manifest_install_url(manifest: SlackManifest) -> str:
    """Return the Slack ``api.slack.com/apps`` URL pre-filled with *manifest*.

    Slack may ignore the ``manifest_json`` query parameter depending on
    the workspace auth state. :func:`~teatree.cli.slack_setup.slack_bot_setup`
    prints the manifest JSON as a fallback so the user can always paste it
    manually.
    """
    encoded = urllib.parse.quote(json.dumps(manifest, separators=(",", ":")))
    return f"{_SLACK_CREATE_APP_URL}{encoded}"


def app_manifest_editor_url(app_id: str) -> str:
    """Deep link to the app's manifest editor (degraded-path target)."""
    return f"https://api.slack.com/apps/{app_id}/app-manifest"


def app_install_url(app_id: str) -> str:
    """Deep link to the app's install page (the one manual OAuth-consent step)."""
    return f"https://api.slack.com/apps/{app_id}/install-on-team"


def _slack_app_api(method: str, payload: dict[str, Any], *, token: str) -> dict[str, Any]:
    """POST to ``https://slack.com/api/<method>`` with a bearer *token*."""
    response = httpx.post(
        f"https://slack.com/api/{method}",
        headers={"Authorization": f"Bearer {token}"},
        data=payload,
        timeout=30,
    )
    response.raise_for_status()
    return dict(response.json())


def export_manifest(*, app_id: str, config_token: str) -> SlackManifest:
    """Return the Slack app's current manifest via ``apps.manifest.export``."""
    result = _slack_app_api("apps.manifest.export", {"app_id": app_id}, token=config_token)
    if not result.get("ok"):
        raise SlackManifestError(str(result.get("error", "unknown error")))
    return dict(result["manifest"])


def update_manifest(*, app_id: str, manifest: SlackManifest, config_token: str) -> dict[str, Any]:
    """Apply *manifest* to the app in place via ``apps.manifest.update``."""
    result = _slack_app_api(
        "apps.manifest.update",
        {"app_id": app_id, "manifest": json.dumps(manifest)},
        token=config_token,
    )
    if not result.get("ok"):
        raise SlackManifestError(str(result.get("error", "unknown error")))
    return result


def rotate_config_token(*, refresh_token: str) -> tuple[str, str]:
    """Rotate the app-config token pair via ``tooling.tokens.rotate``.

    Returns ``(access_token, refresh_token)``.
    """
    result = _slack_app_api("tooling.tokens.rotate", {"refresh_token": refresh_token}, token=refresh_token)
    if not result.get("ok"):
        raise SlackManifestError(str(result.get("error", "unknown error")))
    return str(result["token"]), str(result["refresh_token"])


def _scope_set(manifest: SlackManifest, kind: str) -> set[str]:
    return set(manifest.get("oauth_config", {}).get("scopes", {}).get(kind, []))


def manifests_equivalent(a: SlackManifest, b: SlackManifest) -> bool:
    """Compare only the teatree-owned manifest fields, order-insensitively."""

    def shape(m: SlackManifest) -> tuple[Any, ...]:
        settings = m.get("settings", {})
        return (
            _scope_set(m, "bot"),
            _scope_set(m, "user"),
            frozenset(settings.get("event_subscriptions", {}).get("bot_events", [])),
            settings.get("socket_mode_enabled"),
            m.get("display_information", {}).get("name"),
        )

    return shape(a) == shape(b)


__all__ = [
    "_BOT_ONLY_SCOPES",
    "_CONFIG_REFRESH_REF",
    "_CONFIG_TOKEN_REF",
    "_USER_SCOPES",
    "SlackManifest",
    "SlackManifestError",
    "_slack_app_api",
    "_user_scopes_carry_no_bot_only_scope",
    "app_install_url",
    "app_manifest_editor_url",
    "build_manifest",
    "export_manifest",
    "manifest_install_url",
    "manifests_equivalent",
    "rotate_config_token",
    "update_manifest",
]
