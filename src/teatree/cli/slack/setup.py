"""``t3 setup slack-bot`` — interactive walkthrough for per-overlay Slack apps.

Implements BLUEPRINT § 3.6: scaffold a Slack app from a teatree-owned manifest,
capture bot + app-level tokens into ``pass``, write the user's Slack id into the
DB ``overlays`` registry, and smoke-test the bot with a round-trip DM that the
user acknowledges with a ``:white_check_mark:`` reaction.

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

Manifest-building and Slack API helpers live in
:mod:`teatree.cli.slack.manifest`; this module re-exports them so existing
callers are unaffected.
"""

import json
import re
import time
import webbrowser
from typing import NoReturn

import typer

from teatree.backends.slack.bot import SlackBotBackend
from teatree.cli.slack.app_resolve import overlay_scope_profile, read_overlay_field, write_overlay_fields
from teatree.cli.slack.manifest import (
    _BOT_ONLY_SCOPES,
    _CONFIG_REFRESH_REF,
    _CONFIG_TOKEN_REF,
    _USER_SCOPES,
    SlackManifest,
    SlackManifestError,
    _slack_app_api,
    _user_scopes_carry_no_bot_only_scope,
    app_install_url,
    app_manifest_editor_url,
    build_manifest,
    export_manifest,
    manifest_install_url,
    manifests_equivalent,
    rotate_config_token,
    update_manifest,
)
from teatree.cli.slack.token_store import SlackTokenWriteError, app_token_slot, bot_token_slot, store_slack_token
from teatree.config import discover_overlays
from teatree.utils.django_bootstrap import ensure_django
from teatree.utils.secrets import read_pass, write_pass

# Re-exported so existing ``from teatree.cli.slack.setup import …`` callers
# keep working without touching their imports.
__all__ = [
    "_BOT_ONLY_SCOPES",
    "_CONFIG_REFRESH_REF",
    "_CONFIG_TOKEN_REF",
    "_USER_SCOPES",
    "SlackManifest",
    "SlackManifestError",
    "_export_with_rotation",
    "_slack_app_api",
    "_user_scopes_carry_no_bot_only_scope",
    "app_install_url",
    "app_manifest_editor_url",
    "build_manifest",
    "export_manifest",
    "manifest_install_url",
    "manifests_equivalent",
    "rotate_config_token",
    "slack_bot_setup",
    "update_manifest",
    "write_overlay_settings",
]

_APP_ID_RE = re.compile(r"^A[A-Z0-9]{6,}$")
_BOT_TOKEN_RE = re.compile(r"^xoxb-[A-Za-z0-9-]+$")
_APP_TOKEN_RE = re.compile(r"^xapp-[A-Za-z0-9-]+$")
_USER_ID_RE = re.compile(r"^[UW][A-Z0-9]{6,}$")

_SMOKE_TEST_TIMEOUT_SECONDS = 120
_SMOKE_TEST_POLL_SECONDS = 3
_SMOKE_TEST_REACTION = "white_check_mark"


def write_overlay_settings(
    overlay_name: str,
    *,
    slack_user_id: str,
    slack_token_ref: str,
    slack_app_id: str = "",
) -> None:
    """Persist Slack settings on *overlay_name*'s entry of the DB ``overlays`` registry."""
    fields = {
        "messaging_backend": "slack",
        "slack_user_id": slack_user_id,
        "slack_token_ref": slack_token_ref,
    }
    if slack_app_id:
        fields["slack_app_id"] = slack_app_id
    write_overlay_fields(overlay_name, fields)


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


def _prompt_app_id() -> str:
    while True:
        value = typer.prompt("Slack app id (e.g. A01ABCD1234)").strip()
        if _APP_ID_RE.match(value):
            return value
        typer.echo("      Slack app ids start with 'A' followed by uppercase alphanumerics.")


def _run_degraded_path(*, overlay: str, app_id: str, token_ref: str, skip_smoke_test: bool) -> None:
    """No config token stored — print the manifest + editor deep link, then smoke-test."""
    manifest = build_manifest(overlay_name=overlay, scope_profile=overlay_scope_profile(overlay))
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
    _finish_with_smoke_test(overlay=overlay, app_id=app_id, token_ref=token_ref, skip_smoke_test=skip_smoke_test)


def _finish_with_smoke_test(*, overlay: str, app_id: str, token_ref: str, skip_smoke_test: bool) -> None:
    user_id = read_overlay_field(overlay, "slack_user_id")
    write_overlay_settings(
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


def _run_update_path(*, overlay: str, app_id: str, token_ref: str, skip_smoke_test: bool) -> None:
    """Update an existing app's manifest in place via Slack's manifest API."""
    if not read_pass(_CONFIG_TOKEN_REF):
        _run_degraded_path(overlay=overlay, app_id=app_id, token_ref=token_ref, skip_smoke_test=skip_smoke_test)
        return

    desired = build_manifest(overlay_name=overlay, scope_profile=overlay_scope_profile(overlay))
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
    _finish_with_smoke_test(overlay=overlay, app_id=app_id, token_ref=token_ref, skip_smoke_test=skip_smoke_test)


def _print_create_instructions(overlay: str) -> None:
    manifest = build_manifest(overlay_name=overlay, scope_profile=overlay_scope_profile(overlay))
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


def _run_token_walkthrough(*, overlay: str, token_ref: str, reset: bool, skip_smoke_test: bool) -> None:
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
        overlay,
        slack_user_id=user_id,
        slack_token_ref=token_ref,
        slack_app_id=new_app_id,
    )
    typer.echo(f"OK    Recorded slack_user_id and slack_token_ref for overlay `{overlay}` in the DB overlays registry.")

    typer.echo("Step 4/4 — Smoke test.")
    if skip_smoke_test:
        typer.echo("      Skipped per `--skip-smoke-test`.")
        return
    if not _smoke_test(bot_token=bot_token, user_id=user_id):
        raise typer.Exit(code=1)


def _migrate_token_ref(
    overlay: str,
    old_ref: str,
    new_ref: str,
    *,
    skip_smoke_test: bool,
) -> NoReturn:
    """Copy tokens from *old_ref* slots to *new_ref* slots, update the registry, and smoke-test.

    Raises :class:`typer.Exit` (code 1) when either old-slot value is absent
    (nothing to migrate — the user must store tokens first).  The copy uses
    :func:`store_slack_token` so validation and backup-before-overwrite
    invariants are preserved.  After the copy the registry ``slack_token_ref``
    is updated to *new_ref* and an optional smoke test is run, then the function
    raises :class:`typer.Exit` (code 0) so the caller can return immediately.
    """
    old_bot = read_pass(f"{old_ref}-bot")
    old_app = read_pass(f"{old_ref}-app")
    if not old_bot or not old_app:
        typer.echo(
            f"ERROR Cannot migrate `{old_ref}` → `{new_ref}`: "
            f"no token stored under `{old_ref}-bot` / `{old_ref}-app`. "
            "Store the tokens first, then rerun."
        )
        raise typer.Exit(code=1)

    typer.echo(f"INFO  Migrating tokens from `{old_ref}` → `{new_ref}` …")
    try:
        store_slack_token(bot_token_slot(new_ref), old_bot, echo=typer.echo)
        store_slack_token(app_token_slot(new_ref), old_app, echo=typer.echo)
    except SlackTokenWriteError as exc:
        typer.echo(f"ERROR Migration failed: {exc}")
        raise typer.Exit(code=1) from exc
    typer.echo(f"OK    Tokens migrated to `{new_ref}-bot` and `{new_ref}-app`.")

    user_id = read_overlay_field(overlay, "slack_user_id")
    app_id = read_overlay_field(overlay, "slack_app_id")
    write_overlay_settings(
        overlay,
        slack_user_id=user_id,
        slack_token_ref=new_ref,
        slack_app_id=app_id,
    )
    typer.echo(f"OK    Rewrote overlay `{overlay}` slack_token_ref to `{new_ref}` in the DB overlays registry.")

    typer.echo("Step — Smoke test.")
    if skip_smoke_test:
        typer.echo("      Skipped per `--skip-smoke-test`.")
        raise typer.Exit(code=0)
    bot_token = read_pass(f"{new_ref}-bot")
    if not _smoke_test(bot_token=bot_token, user_id=user_id):
        raise typer.Exit(code=1)
    raise typer.Exit(code=0)


def slack_bot_setup(
    *,
    overlay: str = typer.Option(..., "--overlay", help="Overlay name as registered in the DB overlays registry."),
    reset: bool = typer.Option(False, "--reset", help="Rotate the existing bot + app tokens; skip the manifest URL."),
    update: bool = typer.Option(
        False,
        "--update",
        help="Force the in-place manifest update path (prompts for the app id if none recorded).",
    ),
    skip_smoke_test: bool = typer.Option(False, "--skip-smoke-test", help="Skip the round-trip DM verification."),
) -> None:
    """Register or update a per-overlay Slack bot and store its tokens via ``pass``."""
    ensure_django()
    _validate_overlay(overlay)
    token_ref = f"teatree/{overlay}/slack"

    existing_ref = read_overlay_field(overlay, "slack_token_ref")
    if existing_ref and existing_ref != token_ref:
        _migrate_token_ref(overlay, existing_ref, token_ref, skip_smoke_test=skip_smoke_test)

    if not reset:
        recorded_app_id = read_overlay_field(overlay, "slack_app_id")
        if recorded_app_id or update:
            from teatree.cli.slack.app_resolve import resolve_overlay_app_id  # noqa: PLC0415 — avoids a slack cycle

            app_id = resolve_overlay_app_id(overlay, token_ref=token_ref) or _prompt_app_id()
            try:
                _run_update_path(
                    overlay=overlay,
                    app_id=app_id,
                    token_ref=token_ref,
                    skip_smoke_test=skip_smoke_test,
                )
            except SlackManifestError as exc:
                typer.echo(f"ERROR Slack manifest API failed: {exc}")
                raise typer.Exit(code=1) from exc
            return

    _run_token_walkthrough(
        overlay=overlay,
        token_ref=token_ref,
        reset=reset,
        skip_smoke_test=skip_smoke_test,
    )
