"""``t3 setup slack-provision`` — one command for the full Slack app lifecycle.

Setting up Slack for an overlay used to be a string of separate commands —
``slack-bot`` (per overlay, prompting for an app id it could resolve itself),
``slack-user-token`` (the shared xoxp capture), plus manual channel invites —
and a missing user scope (``reactions:write``) silently blocked reacting. This
command runs the whole lifecycle for one overlay, or every Slack overlay, in
one idempotent pass (#1686):

1. Resolve the app id from config, else derive it from the bot token, else
prompt — persisting it so the derivation runs once
(:mod:`teatree.cli.slack.app_resolve`).
2. Push the desired manifest (all bot + user scopes, including
``reactions:write``) via Slack's ``apps.manifest.update`` using the app-config
token in ``pass``.
3. Print the exact OAuth (re)install URL — the single irreducible human step —
and open it in the browser.
4. Join the bot to its review-broadcast channels
(:mod:`teatree.cli.slack.channel_provisioning`) so its first post / reaction
there does not fail ``not_in_channel`` (the canary).
5. Provision the bot's IM channel (:mod:`teatree.cli.slack.dm_provisioning`).
6. Capture / verify the shared xoxp user token
(:mod:`teatree.cli.slack.user_token_setup`) so ``reactions:write`` is granted.

The browser OAuth-consent click is the only step that cannot be automated; the
command prints its exact URL and opens it.
"""

import json
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx
import typer

from teatree.cli.slack.app_resolve import read_overlay_registry, resolve_overlay_app_id
from teatree.cli.slack.channel_provisioning import ChannelJoinResult, join_review_channels, render_join_result
from teatree.cli.slack.dm_provisioning import ProvisionResult, provision_overlay_dm_channel
from teatree.cli.slack.setup import (
    _APP_ID_RE,
    _CONFIG_TOKEN_REF,
    SlackManifestError,
    _export_with_rotation,
    app_install_url,
    app_manifest_editor_url,
    build_manifest,
    manifests_equivalent,
    update_manifest,
)
from teatree.cli.slack.user_token_setup import REQUIRED_USER_SCOPES
from teatree.config import discover_overlays
from teatree.core.overlay_loader import get_overlay
from teatree.utils.django_bootstrap import ensure_django
from teatree.utils.secrets import read_pass


@dataclass(slots=True)
class OverlayProvisionReport:
    """Per-overlay record of what :func:`provision_overlay` did."""

    overlay_name: str
    app_id: str = ""
    manifest_action: str = "skipped"
    install_url: str = ""
    channel_results: list[ChannelJoinResult] = field(default_factory=list)
    dm_result: ProvisionResult | None = None
    notes: list[str] = field(default_factory=list)


def _resolve_app_id(*, overlay: str, echo: Callable[[str], None]) -> str:
    app_id = resolve_overlay_app_id(overlay)
    if app_id:
        return app_id
    echo(f"WARN  No slack_app_id for `{overlay}` in the registry and none derivable from the bot token.")
    value = typer.prompt(f"Slack app id for `{overlay}` (e.g. A01ABCD1234)").strip()
    if not _APP_ID_RE.match(value):
        echo("ERROR Invalid Slack app id format.")
        raise typer.Exit(code=1)
    from teatree.cli.slack.app_resolve import write_overlay_fields  # noqa: PLC0415 — avoids a slack-package cycle

    write_overlay_fields(overlay, {"slack_app_id": value})
    return value


def _push_manifest(*, overlay: str, app_id: str, echo: Callable[[str], None]) -> str:
    """Push the desired manifest; return the action taken ("updated"/"current"/"degraded")."""
    if not read_pass(_CONFIG_TOKEN_REF):
        echo("!! DEGRADED — manifest NOT pushed; user scopes are NOT set on the app.")
        echo("!! Until you add them, the app has ZERO user scopes — the personal xoxp")
        echo("!! token cannot post or react in Slack-Connect channels.")
        echo(f"   No Slack app-config token in `pass {_CONFIG_TOKEN_REF}` — cannot auto-update the manifest.")
        echo(f"   FIX NOW (manual): open {app_manifest_editor_url(app_id)}")
        echo("   and add these user scopes under oauth_config.scopes.user, then reinstall:")
        echo(f"        {', '.join(REQUIRED_USER_SCOPES)}")
        echo("   To automate next time, store a config token:")
        echo(f"        pass insert {_CONFIG_TOKEN_REF}")
        return "degraded"
    desired = build_manifest(overlay_name=overlay)
    current = _export_with_rotation(app_id=app_id)
    if manifests_equivalent(current, desired):
        echo("OK    Manifest already current (all scopes present).")
        return "current"
    result = update_manifest(app_id=app_id, manifest=desired, config_token=read_pass(_CONFIG_TOKEN_REF))
    echo(f"OK    Manifest updated (permissions changed: {result.get('permissions_updated', '?')}).")
    return "updated"


def _print_oauth_step(*, app_id: str, manifest_action: str, echo: Callable[[str], None]) -> str:
    """Print the one irreducible human step: the OAuth (re)install URL."""
    install_url = app_install_url(app_id)
    echo("")
    echo("ACTION  The one manual step — (re)install to re-consent the scopes:")
    echo(f"        {install_url}")
    if manifest_action in {"updated", "degraded"}:
        echo(f"        bot + user scopes (incl. reactions:write): {', '.join(REQUIRED_USER_SCOPES)}")
    return install_url


def _broadcast_channels(overlay: str) -> list[tuple[str, str]]:
    """Return the overlay's review-broadcast channels, or [] when unresolvable."""
    try:
        overlay_obj = get_overlay(overlay)
    except Exception:  # noqa: BLE001 — overlay may not be a registered Python class
        return []
    return list(overlay_obj.config.get_review_broadcast_channels())


def _provision_channels(
    *,
    overlay: str,
    echo: Callable[[str], None],
) -> list[ChannelJoinResult]:
    from teatree.cli.slack.app_resolve import read_overlay_field  # noqa: PLC0415 — avoids a slack-package cycle

    channels = _broadcast_channels(overlay)
    if not channels:
        return []
    token_ref = read_overlay_field(overlay, "slack_token_ref")
    bot_token = read_pass(f"{token_ref}-bot") if token_ref else ""
    if not bot_token:
        echo("WARN  No bot token — skipping review-channel join.")
        return []
    from teatree.backends.slack.bot import SlackBotBackend  # noqa: PLC0415 — deferred: keeps CLI startup light

    backend = SlackBotBackend(bot_token=bot_token)
    results = join_review_channels(backend=backend, channels=channels)
    for result in results:
        render_join_result(result, echo)
    return results


def provision_overlay(
    *,
    overlay: str,
    echo: Callable[[str], None],
    open_browser: bool = True,
) -> OverlayProvisionReport:
    """Run the full Slack lifecycle for one *overlay*; return what it did."""
    echo(f"── Provisioning Slack for overlay `{overlay}` ──")
    report = OverlayProvisionReport(overlay_name=overlay)

    app_id = _resolve_app_id(overlay=overlay, echo=echo)
    report.app_id = app_id
    echo(f"OK    App id: {app_id} (manifest editor: {app_manifest_editor_url(app_id)})")

    try:
        report.manifest_action = _push_manifest(overlay=overlay, app_id=app_id, echo=echo)
    except SlackManifestError as exc:
        echo(f"ERROR Slack manifest API failed: {exc}")
        report.notes.append(f"manifest_error: {exc}")
        report.manifest_action = "error"

    report.install_url = _print_oauth_step(app_id=app_id, manifest_action=report.manifest_action, echo=echo)
    if open_browser:
        webbrowser.open(report.install_url)

    report.channel_results = _provision_channels(overlay=overlay, echo=echo)

    dm_result = provision_overlay_dm_channel(overlay_name=overlay)
    report.dm_result = dm_result
    _render_dm(dm_result, echo)

    return report


def _render_dm(result: ProvisionResult, echo: Callable[[str], None]) -> None:
    if result.status is ProvisionResult.PROVISIONED:
        echo(f"OK    Provisioned bot IM channel ({result.channel_id}).")
    elif result.status is ProvisionResult.SKIPPED_ALREADY_PROVISIONED:
        echo(f"OK    Bot IM channel already provisioned ({result.channel_id}).")
    elif result.status in {ProvisionResult.SKIPPED_NO_BOT_TOKEN, ProvisionResult.SKIPPED_NO_USER_ID}:
        echo(f"WARN  IM channel not provisioned: {result.detail}.")
    elif result.status is ProvisionResult.FAILED_OPEN_DM:
        echo(f"ERROR IM channel provisioning failed: {result.detail}.")


def _slack_overlays() -> list[str]:
    """Return the names of every overlay configured for the Slack backend in the DB registry."""
    return [
        name
        for name, block in read_overlay_registry().items()
        if isinstance(block, dict) and str(block.get("messaging_backend", "")) == "slack"
    ]


def _verify_user_token(echo: Callable[[str], None]) -> None:
    """Report whether the shared xoxp token already carries every required scope."""
    from teatree.cli.slack.user_token_setup import (  # noqa: PLC0415 — avoids a slack-package cycle
        USER_TOKEN_PASS_KEY,
        TokenScopeError,
        fetch_token_scopes,
        missing_scopes,
    )

    token = read_pass(USER_TOKEN_PASS_KEY)
    if not token:
        echo(f"ACTION  No xoxp user token at `pass {USER_TOKEN_PASS_KEY}` — run `t3 setup slack-user-token`.")
        return
    try:
        granted = fetch_token_scopes(token)
    except (httpx.HTTPError, TokenScopeError) as exc:
        echo(f"WARN  Could not verify the xoxp user token scopes: {exc}.")
        return
    missing = missing_scopes(granted, REQUIRED_USER_SCOPES)
    if missing:
        echo(f"ACTION  xoxp user token is missing scope(s): {', '.join(missing)}.")
        echo("        Reinstall via the URL(s) above, then run `t3 setup slack-user-token`.")
    else:
        echo("OK    xoxp user token already carries every required scope (incl. reactions:write).")


def slack_provision(
    *,
    overlay: str = typer.Option(
        "",
        "--overlay",
        help="Overlay to provision (default: every Slack-backed overlay in the DB registry).",
    ),
    open_browser: bool = typer.Option(
        True,
        "--open-browser/--no-open-browser",
        help="Open the OAuth (re)install URL in the browser.",
    ),
) -> None:
    """Run the full Slack app lifecycle (manifest, scopes, channels, tokens) idempotently."""
    ensure_django()
    if overlay:
        registered = {entry.name for entry in discover_overlays()}
        if overlay not in registered:
            known = ", ".join(sorted(registered)) or "(none registered)"
            typer.echo(f"ERROR Overlay {overlay!r} is not registered. Known overlays: {known}")
            raise typer.Exit(code=1)
        overlays = [overlay]
    else:
        overlays = _slack_overlays()
        if not overlays:
            typer.echo(
                "No Slack-backed overlays found in the DB registry. Run `t3 setup slack-bot --overlay <name>` first."
            )
            raise typer.Exit(code=1)

    reports = [provision_overlay(overlay=name, echo=typer.echo, open_browser=open_browser) for name in overlays]

    typer.echo("")
    typer.echo("── Shared personal xoxp user token ──")
    _verify_user_token(typer.echo)

    typer.echo("")
    typer.echo("── Summary ──")
    for report in reports:
        joined = sum(1 for r in report.channel_results if r.status.value in {"joined", "already_in"})
        typer.echo(
            f"  {report.overlay_name}: app {report.app_id}, manifest {report.manifest_action}, "
            f"{joined}/{len(report.channel_results)} review channels ready."
        )
    typer.echo("")
    typer.echo("Remaining manual step: click Allow on each OAuth (re)install URL above, then")
    typer.echo("run `t3 setup slack-user-token` if any token scope is still missing.")


def manifest_json(overlay: str) -> str:
    """Return the desired manifest JSON for *overlay* (debugging / docs aid)."""
    return json.dumps(build_manifest(overlay_name=overlay), indent=2)


__all__ = [
    "OverlayProvisionReport",
    "manifest_json",
    "provision_overlay",
    "slack_provision",
]
