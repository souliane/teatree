"""``t3 setup slack-bot`` — interactive walkthrough for per-overlay Slack apps.

Implements BLUEPRINT § 3.6: scaffold a Slack app from a teatree-owned manifest,
capture bot + app-level tokens into ``pass``, write the user's Slack id into
``~/.teatree.toml``, and smoke-test the bot with a round-trip DM that the user
acknowledges with a ``:white_check_mark:`` reaction.

The walkthrough never writes a token to disk in plaintext; tokens always go
through ``pass``. Re-running with ``--reset`` rotates both tokens without
re-prompting for the manifest URL.
"""

import importlib
import json
import re
import time
import urllib.parse
import webbrowser
from pathlib import Path
from typing import Any

import typer

from teatree.backends.slack_bot import SlackBotBackend
from teatree.config import CONFIG_PATH, discover_overlays
from teatree.utils.secrets import write_pass

type SlackManifest = dict[str, Any]

_BOT_SCOPES = [
    "channels:history",
    "channels:read",
    "chat:write",
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
_BOT_EVENTS = ["app_mention", "message.im"]

_SMOKE_TEST_TIMEOUT_SECONDS = 120
_SMOKE_TEST_POLL_SECONDS = 3
_SMOKE_TEST_REACTION = "white_check_mark"

_BOT_TOKEN_RE = re.compile(r"^xoxb-[A-Za-z0-9-]+$")
_APP_TOKEN_RE = re.compile(r"^xapp-[A-Za-z0-9-]+$")
_USER_ID_RE = re.compile(r"^[UW][A-Z0-9]{6,}$")


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
            "bot_user": {"display_name": name, "always_online": True},
        },
        "oauth_config": {"scopes": {"bot": _BOT_SCOPES}},
        "settings": {
            "event_subscriptions": {"bot_events": _BOT_EVENTS},
            "interactivity": {"is_enabled": False},
            "org_deploy_enabled": False,
            "socket_mode_enabled": True,
            "token_rotation_enabled": False,
        },
    }


def manifest_install_url(manifest: SlackManifest) -> str:
    """Return the Slack ``api.slack.com/apps`` URL pre-filled with *manifest*."""
    encoded = urllib.parse.quote(json.dumps(manifest, separators=(",", ":")))
    return f"https://api.slack.com/apps?new_app=1&manifest_json={encoded}"


def write_overlay_settings(
    config_path: Path,
    overlay_name: str,
    *,
    slack_user_id: str,
    slack_bot_token_ref: str,
    messaging_backend: str = "slack",
) -> None:
    """Persist Slack settings on the per-overlay block of *config_path*.

    Uses :mod:`tomlkit` so the rest of the file (other overlays, global
    ``[teatree]`` settings, comments, ordering) is preserved.

    ``tomlkit`` is resolved lazily via :func:`importlib.import_module` so the
    CLI bootstrap stays robust to stale installs that predate the dep being
    added — every other subcommand keeps working even when ``tomlkit`` is
    missing, and the user only sees the ImportError when they actually run
    ``t3 setup slack-bot``.
    """
    tomlkit = importlib.import_module("tomlkit")
    tomlkit_items = importlib.import_module("tomlkit.items")

    document = tomlkit.parse(config_path.read_text(encoding="utf-8")) if config_path.is_file() else tomlkit.document()

    overlays = document.get("overlays")
    if not isinstance(overlays, tomlkit_items.Table):
        overlays = tomlkit.table()
        document["overlays"] = overlays

    overlay_block = overlays.get(overlay_name)
    if not isinstance(overlay_block, tomlkit_items.Table):
        overlay_block = tomlkit.table()
        overlays[overlay_name] = overlay_block

    overlay_block["messaging_backend"] = messaging_backend
    overlay_block["slack_user_id"] = slack_user_id
    overlay_block["slack_bot_token_ref"] = slack_bot_token_ref

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
    if not write_pass(f"{token_ref}-bot", bot_token):
        typer.echo("ERROR Failed to store bot token via `pass`.")
        raise typer.Exit(code=1)
    if not write_pass(f"{token_ref}-app", app_token):
        typer.echo("ERROR Failed to store app token via `pass`.")
        raise typer.Exit(code=1)
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


def slack_bot_setup(
    *,
    overlay: str = typer.Option(..., "--overlay", help="Overlay name as registered in `~/.teatree.toml`."),
    reset: bool = typer.Option(False, "--reset", help="Rotate the existing bot + app tokens; skip the manifest URL."),
    skip_smoke_test: bool = typer.Option(False, "--skip-smoke-test", help="Skip the round-trip DM verification."),
    config_path: Path = typer.Option(
        CONFIG_PATH, "--config", help="Path to teatree config (default: ~/.teatree.toml)."
    ),
) -> None:
    """Register a per-overlay Slack bot and store its tokens via ``pass``."""
    _validate_overlay(overlay)
    token_ref = f"teatree/{overlay}/slack"

    if not reset:
        manifest = build_manifest(overlay_name=overlay)
        url = manifest_install_url(manifest)
        typer.echo("Step 1/4 — Open the Slack app-manifest URL and install the app to your workspace:")
        typer.echo(f"      {url}")
        webbrowser.open(url)
        typer.echo("      (The browser should open automatically. Copy the URL above if it didn't.)")
    else:
        typer.echo("Step 1/4 — Reset mode: skipping manifest URL.")

    typer.echo("Step 2/4 — Paste the bot token (`xoxb-…`) and app-level token (`xapp-…`).")
    bot_token = _prompt_token("bot token", _BOT_TOKEN_RE)
    app_token = _prompt_token("app-level token", _APP_TOKEN_RE)
    _store_tokens(token_ref, bot_token=bot_token, app_token=app_token)

    typer.echo("Step 3/4 — Record your Slack user id so the bot knows who to talk to.")
    user_id = _prompt_user_id()
    write_overlay_settings(
        config_path,
        overlay,
        slack_user_id=user_id,
        slack_bot_token_ref=token_ref,
    )
    typer.echo(f"OK    Wrote `[overlays.{overlay}]` slack_user_id and slack_bot_token_ref to {config_path}.")

    typer.echo("Step 4/4 — Smoke test.")
    if skip_smoke_test:
        typer.echo("      Skipped per `--skip-smoke-test`.")
        return
    if not _smoke_test(bot_token=bot_token, user_id=user_id):
        raise typer.Exit(code=1)
