"""``t3 setup slack-user-token`` — re-scope the personal Slack OAuth (xoxp) token.

The personal token at ``pass slack/user-oauth-token`` is the credential that
``SlackBotBackend`` uses to post and react in Slack-Connect externally-shared
channels (where the bot token is rejected with
``mcp_externally_shared_channel_restricted``). Several scopes that recent
backend work depends on — notably ``reactions:write`` and ``chat:write`` —
are missing from existing installations because they were not yet in the
manifest when the user last consented. (``chat:write.public`` and
``chat:write.customize`` are bot-only and are intentionally absent from the
user-scope set; see ``slack_setup._BOT_ONLY_SCOPES``.)

This command walks the user through reinstalling the Slack app to re-prompt
OAuth consent for an updated user-scope set, then captures the new ``xoxp-…``
token into ``pass slack/user-oauth-token`` after verifying the token actually
carries the requested scopes (Slack returns the granted scope set in the
``x-oauth-scopes`` response header of ``auth.test``).

This is intentionally separate from ``t3 setup slack-bot``: the bot command
manages the manifest + bot/app tokens; this command focuses on the personal
xoxp token capture and scope verification step.
"""

import webbrowser
from collections.abc import Callable

import httpx
import typer

# Re-export under the local underscored name for backward-compat with prior
# imports inside this module. Source of truth lives in
# ``teatree.backends.slack.token_validation`` so the runtime gate at
# backend construction (#1285) and the capture-time gate here agree on
# the exact regex — drift between the two would let a token shape pass
# capture but fail at construction, or vice versa.
from teatree.backends.slack.token_validation import USER_TOKEN_RE as _USER_TOKEN_RE
from teatree.cli.slack_app_resolve import derive_app_id_from_token, read_overlay_registry
from teatree.cli.slack_setup import _USER_SCOPES
from teatree.cli.slack_token_store import BOT_TOKEN_SLOT, USER_TOKEN_SLOT, SlackTokenWriteError, store_slack_token
from teatree.utils.django_bootstrap import ensure_django
from teatree.utils.secrets import read_pass

USER_TOKEN_PASS_KEY = "slack/user-oauth-token"  # noqa: S105 — pass key name, not a secret
BOT_TOKEN_PASS_KEY = "slack/bot-token"  # noqa: S105 — pass key name, not a secret


def app_oauth_url(app_id: str) -> str:
    """Deep link to the app's OAuth & Permissions page — the User OAuth Token lives there."""
    return f"https://api.slack.com/apps/{app_id}/oauth"


# Single source of truth: the manifest's _USER_SCOPES in slack_setup.py
# declares what Slack will grant on reinstall, and this command verifies the
# returned token carries exactly that set. Drift between the two would either
# (a) trip the missing-scope check on every run (manifest narrower than
# REQUIRED), or (b) silently approve under-scoped tokens (REQUIRED narrower
# than manifest). Deriving REQUIRED from the manifest constant prevents both.
REQUIRED_USER_SCOPES: list[str] = sorted(_USER_SCOPES)


class TokenScopeError(RuntimeError):
    """Returned token does not carry every scope the command requested."""


def _prompt_user_token() -> str:
    while True:
        value = typer.prompt("Paste xoxp user token from OAuth & Permissions", hide_input=True).strip()
        if _USER_TOKEN_RE.match(value):
            return value
        typer.echo("      Invalid xoxp token format — must look like 'xoxp-…'. Try again.")


def fetch_token_scopes(token: str) -> list[str]:
    response = httpx.post(
        "https://slack.com/api/auth.test",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    response.raise_for_status()
    body = response.json()
    if not body.get("ok"):
        error = body.get("error", "unknown error")
        message = f"auth.test returned ok=False: {error}"
        raise TokenScopeError(message)
    header = response.headers.get("x-oauth-scopes", "")
    return sorted(s.strip() for s in header.split(",") if s.strip())


def missing_scopes(actual: list[str], required: list[str]) -> list[str]:
    actual_set = set(actual)
    return sorted(scope for scope in required if scope not in actual_set)


def added_scopes(actual: list[str], previous: list[str]) -> list[str]:
    return sorted(set(actual) - set(previous))


def _read_existing_scopes() -> list[str]:
    existing = read_pass(USER_TOKEN_PASS_KEY)
    if not existing:
        return []
    try:
        return fetch_token_scopes(existing)
    except (httpx.HTTPError, TokenScopeError):
        return []


def _confirm_overwrite(*, reset: bool) -> bool:
    if reset:
        return True
    if not read_pass(USER_TOKEN_PASS_KEY):
        return True
    return typer.confirm(
        f"`pass {USER_TOKEN_PASS_KEY}` already exists. Overwrite with a freshly-authorized token?",
        default=False,
    )


def _print_reauthorize_instructions(overlay_app_id: str) -> None:
    oauth_url = app_oauth_url(overlay_app_id) if overlay_app_id else ""
    typer.echo("Step 1/3 — Reinstall the Slack app to re-prompt OAuth consent.")
    typer.echo("")
    typer.echo(f"      Requested user scopes ({len(REQUIRED_USER_SCOPES)}):")
    for scope in REQUIRED_USER_SCOPES:
        typer.echo(f"        - {scope}")
    typer.echo("")
    if oauth_url:
        typer.echo(f"      Opening the OAuth & Permissions page: {oauth_url}")
        webbrowser.open(oauth_url)
    else:
        typer.echo("      No slack_app_id recorded or derivable — open your Slack app manually:")
        typer.echo("        https://api.slack.com/apps")
    typer.echo("")
    typer.echo("      Before reinstalling, make sure the app's manifest declares ALL the scopes")
    typer.echo("      listed above under oauth_config.scopes.user. If a scope is missing from the")
    typer.echo("      manifest, Slack will not re-prompt for it. Update the manifest first via")
    typer.echo("      `t3 setup slack-bot --update` if needed.")
    typer.echo("")
    typer.echo("      After clicking Allow, copy the new User OAuth Token (xoxp-…) from the")
    typer.echo("      app's 'OAuth & Permissions' page and paste it below.")


def _store_and_verify(
    token: str,
    previous_scopes: list[str],
    *,
    echo: Callable[[str], None],
) -> tuple[list[str], list[str]]:
    granted = fetch_token_scopes(token)
    missing = missing_scopes(granted, REQUIRED_USER_SCOPES)
    if missing:
        joined = ", ".join(missing)
        message = (
            f"Token is missing required scope(s): {joined}. "
            f"Re-run after updating the Slack app manifest and reinstalling."
        )
        raise TokenScopeError(message)
    try:
        store_slack_token(USER_TOKEN_SLOT, token, echo=echo)
    except SlackTokenWriteError as exc:
        raise TokenScopeError(str(exc)) from exc
    added = added_scopes(granted, previous_scopes)
    return granted, added


def _resolve_overlay_app_id() -> str:
    """Return the first ``slack_app_id`` recorded on any overlay in the DB registry, else ``""``.

    The shared xoxp token is overlay-agnostic, so any overlay's app id is a
    valid OAuth-page target. Per-overlay derivation lives in
    :func:`teatree.cli.slack_app_resolve.resolve_overlay_app_id`.
    """
    for block in read_overlay_registry().values():
        app_id = block.get("slack_app_id") if isinstance(block, dict) else None
        if app_id:
            return str(app_id)
    return ""


def _detect_and_backup_xoxb_mis_install(*, echo: Callable[[str], None]) -> None:
    """Back up a bot token mis-installed at the user-token pass key.

    If the manifest was installed before user scopes were added, Slack returned
    a bot (``xoxb-…``) token where the user (``xoxp-…``) token belongs. Preserve
    it under ``slack/bot-token`` (for the read-only scanner) before the reinstall
    flow overwrites the user-token slot.
    """
    current = read_pass(USER_TOKEN_PASS_KEY)
    if not current.startswith("xoxb-"):
        return
    if read_pass(BOT_TOKEN_PASS_KEY) == current:
        return
    echo(
        "      bot token mis-install detected at pass "
        f"{USER_TOKEN_PASS_KEY} — backing up to {BOT_TOKEN_PASS_KEY} before reinstall."
    )
    try:
        store_slack_token(BOT_TOKEN_SLOT, current, echo=echo)
    except SlackTokenWriteError as exc:
        echo(f"WARN  Could not preserve the mis-installed bot token: {exc}")


# Source of truth for the derive-from-token chain is
# ``teatree.cli.slack_app_resolve`` so ``slack-bot``, ``slack-provision`` and
# this command share one implementation (#1686). Re-exported under the local
# name for backward-compat with callers and tests that import it from here.
_derive_app_id_from_bot = derive_app_id_from_token


def slack_user_token_setup(
    *,
    reset: bool = typer.Option(False, "--reset", help="Overwrite the existing token without prompting."),
) -> None:
    """Re-authorize the personal Slack xoxp token and store it via ``pass``."""
    ensure_django()
    _detect_and_backup_xoxb_mis_install(echo=typer.echo)
    previous_scopes = _read_existing_scopes()
    overlay_app_id = _resolve_overlay_app_id()
    if not overlay_app_id:
        overlay_app_id = _derive_app_id_from_bot(read_pass(USER_TOKEN_PASS_KEY) or read_pass(BOT_TOKEN_PASS_KEY))
    _print_reauthorize_instructions(overlay_app_id)

    if not _confirm_overwrite(reset=reset):
        typer.echo("Aborted — existing token left in place.")
        raise typer.Exit(code=1)

    typer.echo("Step 2/3 — Paste the freshly-authorized xoxp token.")
    token = _prompt_user_token()

    typer.echo("Step 3/3 — Verify scopes via auth.test and store via pass.")
    try:
        granted, added = _store_and_verify(token, previous_scopes, echo=typer.echo)
    except TokenScopeError as exc:
        typer.echo(f"ERROR {exc}")
        raise typer.Exit(code=1) from exc
    except httpx.HTTPError as exc:
        typer.echo(f"ERROR auth.test request failed: {exc}")
        raise typer.Exit(code=1) from exc

    suffix = f" (added: {', '.join(added)})" if added else ""
    typer.echo(f"OK    {USER_TOKEN_PASS_KEY} updated with {len(granted)} scope(s){suffix}.")
