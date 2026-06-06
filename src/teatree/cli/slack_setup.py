"""``t3 setup slack-bot`` — interactive walkthrough for per-overlay Slack apps.

Implements BLUEPRINT § 3.6: scaffold a Slack app from a teatree-owned manifest,
capture bot + app-level tokens into ``pass``, write the user's Slack id into
``~/.teatree.toml``, and smoke-test the bot with a round-trip DM that the user
acknowledges with a ``:white_check_mark:`` reaction.

The walkthrough never writes a token to disk in plaintext; tokens always go
through ``pass``. There are three modes.

``--reset`` rotates both bot/app tokens without re-prompting for the manifest
URL — it does **not** apply a manifest scope change.

The default mode auto-detects an existing app: when
``[overlays.<name>].slack_app_id`` is recorded (or ``--update`` is passed) the
command updates that app's manifest in place via Slack's
``apps.manifest.export`` / ``apps.manifest.update`` using org-wide config
tokens stored in ``pass`` (``teatree/slack-app-config-token`` and
``teatree/slack-app-config-refresh``). The single remaining manual step is the
browser OAuth-consent reinstall click at the app-specific deep link.

With no recorded app id and no ``--update``, the original create-from-manifest
walkthrough runs and records the new app id for next time.
"""

import json
import re
import time
import urllib.parse
import webbrowser
from pathlib import Path
from typing import Any

import httpx
import typer

from teatree.backends.slack.bot import SlackBotBackend
from teatree.cli.slack_token_store import SlackTokenWriteError, app_token_slot, bot_token_slot, store_slack_token
from teatree.config import CONFIG_PATH, discover_overlays
from teatree.utils.secrets import read_pass, write_pass

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


def _user_scopes_carry_no_bot_only_scope() -> None:
    leaked = _BOT_ONLY_SCOPES.intersection(_USER_SCOPES)
    if leaked:
        joined = ", ".join(sorted(leaked))
        message = f"_USER_SCOPES contains bot-only scope(s) Slack rejects on a user token: {joined}"
        raise AssertionError(message)


_user_scopes_carry_no_bot_only_scope()
_BOT_EVENTS = ["app_mention", "message.im"]

_SMOKE_TEST_TIMEOUT_SECONDS = 120
_SMOKE_TEST_POLL_SECONDS = 3
_SMOKE_TEST_REACTION = "white_check_mark"

_BOT_TOKEN_RE = re.compile(r"^xoxb-[A-Za-z0-9-]+$")
_APP_TOKEN_RE = re.compile(r"^xapp-[A-Za-z0-9-]+$")
_USER_ID_RE = re.compile(r"^[UW][A-Z0-9]{6,}$")
_APP_ID_RE = re.compile(r"^A[A-Z0-9]{6,}$")


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
    the workspace auth state. :func:`slack_bot_setup` prints the manifest
    JSON as a fallback so the user can always paste it manually.
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


def write_overlay_settings(
    config_path: Path,
    overlay_name: str,
    *,
    slack_user_id: str,
    slack_token_ref: str,
    slack_app_id: str = "",
) -> None:
    """Persist Slack settings on the per-overlay block of *config_path*.

    Uses :mod:`tomlkit` so the rest of the file (other overlays, global
    ``[teatree]`` settings, comments, ordering) is preserved. ``tomlkit`` is
    imported inline so that a stale teatree install that pre-dates the dep
    being added doesn't crash the rest of the CLI on bootstrap — the failure
    surfaces only to callers of the ``t3 setup slack-bot`` final step.
    """
    import tomlkit  # noqa: PLC0415
    from tomlkit import items as tomlkit_items  # noqa: PLC0415

    document = tomlkit.parse(config_path.read_text(encoding="utf-8")) if config_path.is_file() else tomlkit.document()

    overlays = document.get("overlays")
    if not isinstance(overlays, tomlkit_items.Table):
        overlays = tomlkit.table()
        document["overlays"] = overlays

    overlay_block = overlays.get(overlay_name)
    if not isinstance(overlay_block, tomlkit_items.Table):
        overlay_block = tomlkit.table()
        overlays[overlay_name] = overlay_block

    overlay_block["messaging_backend"] = "slack"
    overlay_block["slack_user_id"] = slack_user_id
    overlay_block["slack_token_ref"] = slack_token_ref
    if slack_app_id:
        overlay_block["slack_app_id"] = slack_app_id

    config_path.write_text(tomlkit.dumps(document), encoding="utf-8")


def _validate_overlay(name: str) -> None:
    overlays = {entry.name for entry in discover_overlays()}
    if name not in overlays:
        known = ", ".join(sorted(overlays)) or "(none registered)"
        typer.echo(f"ERROR Overlay {name!r} is not registered. Known overlays: {known}")
        raise typer.Exit(code=1)


def _prompt_token(label: str, pattern: re.Pattern[str]) -> str:
    while True:
        value = typer.prompt(f"Paste {label}", hide_input=True).strip()
        if pattern.match(value):
            return value
        typer.echo(f"      Invalid {label} format — try again.")


def _prompt_user_id() -> str:
    while True:
        value = typer.prompt("Your Slack user id (e.g. U01ABCD1234)").strip()
        if _USER_ID_RE.match(value):
            return value
        typer.echo("      Slack user ids start with 'U' or 'W' followed by uppercase alphanumerics.")


def _store_tokens(token_ref: str, *, bot_token: str, app_token: str) -> None:
    for slot, value in ((bot_token_slot(token_ref), bot_token), (app_token_slot(token_ref), app_token)):
        try:
            store_slack_token(slot, value, echo=typer.echo)
        except SlackTokenWriteError as exc:
            typer.echo(f"ERROR {exc}")
            raise typer.Exit(code=1) from exc
    typer.echo(f"OK    Stored bot + app tokens under `{token_ref}-bot` and `{token_ref}-app`.")


def _smoke_test(*, bot_token: str, user_id: str) -> bool:
    """Send a DM and wait for the user to react with ``:white_check_mark:``."""
    backend = SlackBotBackend(bot_token=bot_token, user_id=user_id)
    channel = backend.open_dm(user_id)
    if not channel:
        typer.echo("ERROR Could not open a DM channel — token may lack `im:write`.")
        return False
    response = backend.post_message(
        channel=channel,
        text=":wave: Teatree slack-bot setup smoke test — react with :white_check_mark: to confirm.",
    )
    ts = response.get("ts")
    if not isinstance(ts, str):
        typer.echo(f"ERROR Slack rejected the smoke-test DM: {response.get('error', 'unknown error')}.")
        return False
    typer.echo(f"OK    Smoke-test DM delivered (ts={ts}). Waiting up to 2 minutes for :white_check_mark: …")
    deadline = time.monotonic() + _SMOKE_TEST_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if _SMOKE_TEST_REACTION in backend.get_reactions(channel=channel, ts=ts):
            typer.echo("OK    Smoke test complete — bot is wired up correctly.")
            return True
        time.sleep(_SMOKE_TEST_POLL_SECONDS)
    typer.echo("WARN  Timed out waiting for :white_check_mark: — check the bot's app install and try again.")
    return False


def _read_overlay_field(config_path: Path, overlay: str, field: str) -> str:
    """Return ``[overlays.<overlay>].<field>`` or ``""`` when unset."""
    if not config_path.is_file():
        return ""
    import tomlkit  # noqa: PLC0415

    document = tomlkit.parse(config_path.read_text(encoding="utf-8"))
    block = document.get("overlays", {}).get(overlay) or {}
    return str(block.get(field, ""))


def _prompt_app_id() -> str:
    while True:
        value = typer.prompt("Slack app id (e.g. A01ABCD1234)").strip()
        if _APP_ID_RE.match(value):
            return value
        typer.echo("      Slack app ids start with 'A' followed by uppercase alphanumerics.")


def _run_degraded_path(*, overlay: str, app_id: str, token_ref: str, config_path: Path, skip_smoke_test: bool) -> None:
    """No config token stored — print the manifest + editor deep link, then smoke-test."""
    manifest = build_manifest(overlay_name=overlay)
    editor_url = app_manifest_editor_url(app_id)
    typer.echo("WARN  No Slack app-config token stored — can't auto-update the manifest.")
    typer.echo("")
    typer.echo(json.dumps(manifest, indent=2))
    typer.echo("")
    typer.echo(f"      Opening the manifest editor: {editor_url}")
    webbrowser.open(editor_url)
    typer.echo("      → Paste the manifest above, save, then reinstall to re-consent.")
    typer.echo("      To automate this next time, create a Slack app-config token at")
    typer.echo("      https://api.slack.com/reference/manifests#config_tokens and run:")
    typer.echo(f"        pass insert {_CONFIG_TOKEN_REF}")
    typer.echo(f"        pass insert {_CONFIG_REFRESH_REF}")
    _finish_with_smoke_test(
        overlay=overlay, app_id=app_id, token_ref=token_ref, config_path=config_path, skip_smoke_test=skip_smoke_test
    )


def _finish_with_smoke_test(
    *, overlay: str, app_id: str, token_ref: str, config_path: Path, skip_smoke_test: bool
) -> None:
    user_id = _read_overlay_field(config_path, overlay, "slack_user_id")
    write_overlay_settings(
        config_path,
        overlay,
        slack_user_id=user_id,
        slack_token_ref=token_ref,
        slack_app_id=app_id,
    )
    typer.echo("Step 4/4 — Smoke test.")
    if skip_smoke_test:
        typer.echo("      Skipped per `--skip-smoke-test`.")
        return
    bot_token = read_pass(f"{token_ref}-bot")
    if not _smoke_test(bot_token=bot_token, user_id=user_id):
        raise typer.Exit(code=1)


def _export_with_rotation(*, app_id: str) -> SlackManifest:
    """Export the live manifest, rotating the config token once on auth failure."""
    config_token = read_pass(_CONFIG_TOKEN_REF)
    try:
        return export_manifest(app_id=app_id, config_token=config_token)
    except SlackManifestError as exc:
        if str(exc) not in {"invalid_auth", "token_expired"}:
            raise
        refresh_token = read_pass(_CONFIG_REFRESH_REF)
        if not refresh_token:
            typer.echo(
                f"ERROR config token expired; recreate it at {app_manifest_editor_url(app_id)}",
            )
            raise typer.Exit(code=1) from exc
        access, refresh = rotate_config_token(refresh_token=refresh_token)
        write_pass(_CONFIG_TOKEN_REF, access)
        write_pass(_CONFIG_REFRESH_REF, refresh)
        return export_manifest(app_id=app_id, config_token=access)


def _run_update_path(*, overlay: str, app_id: str, token_ref: str, config_path: Path, skip_smoke_test: bool) -> None:
    """Update an existing app's manifest in place via Slack's manifest API."""
    if not read_pass(_CONFIG_TOKEN_REF):
        _run_degraded_path(
            overlay=overlay,
            app_id=app_id,
            token_ref=token_ref,
            config_path=config_path,
            skip_smoke_test=skip_smoke_test,
        )
        return

    desired = build_manifest(overlay_name=overlay)
    current = _export_with_rotation(app_id=app_id)
    if manifests_equivalent(current, desired):
        typer.echo("OK    Manifest already current — nothing to update.")
    else:
        result = update_manifest(app_id=app_id, manifest=desired, config_token=read_pass(_CONFIG_TOKEN_REF))
        typer.echo(f"OK    Manifest updated (permissions changed: {result.get('permissions_updated', '?')}).")
        install_url = app_install_url(app_id)
        typer.echo("ACTION  Reinstall to re-consent the new scopes (the only manual step):")
        typer.echo(f"        {install_url}")
        typer.echo(f"        new user scopes: {', '.join(_USER_SCOPES)}")
        webbrowser.open(install_url)
    _finish_with_smoke_test(
        overlay=overlay, app_id=app_id, token_ref=token_ref, config_path=config_path, skip_smoke_test=skip_smoke_test
    )


def _print_create_instructions(overlay: str) -> None:
    manifest = build_manifest(overlay_name=overlay)
    typer.echo("Step 1/4 — Create the Slack app.")
    typer.echo("")
    typer.echo("      Opening https://api.slack.com/apps …")
    webbrowser.open(manifest_install_url(manifest))
    typer.echo("")
    typer.echo('      → Click "Create New App" → "From an app manifest"')
    typer.echo("      → Pick your workspace → switch to JSON tab → paste this manifest:")
    typer.echo("")
    typer.echo(json.dumps(manifest, indent=2))
    typer.echo("")
    typer.echo('      → Click "Create" → "Install to Workspace" → "Allow"')
    typer.echo("      → Copy the Bot User OAuth Token (xoxb-…) from OAuth & Permissions")
    typer.echo("      → Generate an App-Level Token (xapp-…) from Basic Information")
    typer.echo('        (scope: connections:write, name: "teatree")')
    typer.echo("      → On install, approve the User Token Scopes too (reactions:read,")
    typer.echo("        reactions:write, chat:write, users:read) — required for reactions")
    typer.echo("        and posting in Slack-Connect channels under your identity")


def _print_reset_instructions() -> None:
    typer.echo("Step 1/4 — Reset mode: rotating tokens only (manifest skipped).")
    typer.echo("      NOTE: --reset only rotates the existing tokens. A scope change")
    typer.echo("      (e.g. adding reactions:write to the xoxp user token) does NOT take")
    typer.echo("      effect via --reset — re-run without --reset and re-approve the")
    typer.echo("      manifest in the browser so Slack re-prompts OAuth consent.")


def _run_token_walkthrough(
    *, overlay: str, token_ref: str, config_path: Path, reset: bool, skip_smoke_test: bool
) -> None:
    """Create-or-reset path: capture tokens, record settings, smoke-test."""
    if reset:
        _print_reset_instructions()
    else:
        _print_create_instructions(overlay)

    typer.echo("Step 2/4 — Paste the bot token (`xoxb-…`) and app-level token (`xapp-…`).")
    bot_token = _prompt_token("bot token", _BOT_TOKEN_RE)
    app_token = _prompt_token("app-level token", _APP_TOKEN_RE)
    _store_tokens(token_ref, bot_token=bot_token, app_token=app_token)

    typer.echo("Step 3/4 — Record your Slack user id so the bot knows who to talk to.")
    user_id = _prompt_user_id()
    new_app_id = "" if reset else _prompt_app_id()
    write_overlay_settings(
        config_path,
        overlay,
        slack_user_id=user_id,
        slack_token_ref=token_ref,
        slack_app_id=new_app_id,
    )
    typer.echo(f"OK    Wrote `[overlays.{overlay}]` slack_user_id and slack_token_ref to {config_path}.")

    typer.echo("Step 4/4 — Smoke test.")
    if skip_smoke_test:
        typer.echo("      Skipped per `--skip-smoke-test`.")
        return
    if not _smoke_test(bot_token=bot_token, user_id=user_id):
        raise typer.Exit(code=1)


def slack_bot_setup(
    *,
    overlay: str = typer.Option(..., "--overlay", help="Overlay name as registered in `~/.teatree.toml`."),
    reset: bool = typer.Option(False, "--reset", help="Rotate the existing bot + app tokens; skip the manifest URL."),
    update: bool = typer.Option(
        False,
        "--update",
        help="Force the in-place manifest update path (prompts for the app id if none recorded).",
    ),
    skip_smoke_test: bool = typer.Option(False, "--skip-smoke-test", help="Skip the round-trip DM verification."),
    config_path: Path = typer.Option(
        CONFIG_PATH,
        "--config",
        help="Path to teatree config (default: ~/.teatree.toml).",
    ),
) -> None:
    """Register or update a per-overlay Slack bot and store its tokens via ``pass``."""
    _validate_overlay(overlay)
    token_ref = f"teatree/{overlay}/slack"

    if not reset:
        recorded_app_id = _read_overlay_field(config_path, overlay, "slack_app_id")
        if recorded_app_id or update:
            from teatree.cli.slack_app_resolve import resolve_overlay_app_id  # noqa: PLC0415

            app_id = resolve_overlay_app_id(config_path, overlay, token_ref=token_ref) or _prompt_app_id()
            try:
                _run_update_path(
                    overlay=overlay,
                    app_id=app_id,
                    token_ref=token_ref,
                    config_path=config_path,
                    skip_smoke_test=skip_smoke_test,
                )
            except SlackManifestError as exc:
                typer.echo(f"ERROR Slack manifest API failed: {exc}")
                raise typer.Exit(code=1) from exc
            return

    _run_token_walkthrough(
        overlay=overlay,
        token_ref=token_ref,
        config_path=config_path,
        reset=reset,
        skip_smoke_test=skip_smoke_test,
    )
