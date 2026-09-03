"""``t3 notion setup`` — mint, store and verify the Notion integration token.

There is deliberately no ``--token`` option: a value on argv lands in the host
process table and the shell history, and ``pass insert`` already takes the secret
on stdin, so it never reaches argv on the write either.
"""

import os
import webbrowser
from typing import TYPE_CHECKING

import typer

from teatree.cli.notion_support import fail, notion_client, page_verdict
from teatree.utils.secrets import SecretStoreError, read_pass, write_pass_with_backup

if TYPE_CHECKING:  # pragma: no cover — import-time cost stays off the CLI startup path
    from teatree.backends.notion.client import NotionClient
    from teatree.backends.notion.errors import NotionError

NOTION_INTEGRATIONS_URL = "https://www.notion.so/profile/integrations"

#: Notion grants an integration NONE of these by default, and they live in the
#: integration's own settings — sharing a page does not confer them.
CAPABILITIES: tuple[str, ...] = ("Read content", "Update content", "Insert content", "Read comments")


def notion_setup(
    *,
    overlay: str = typer.Option("", "--overlay", help="Overlay whose token routing to use."),
    page: list[str] = typer.Option(None, "--page", help="Page URL/id to check sharing for. Repeatable."),
    reset: bool = typer.Option(False, "--reset", help="Overwrite a stored token without confirming."),
) -> None:
    """Open Notion's integrations page, store the pasted secret, then verify it end to end."""
    pass_key = _target_pass_key(overlay)
    _announce_integration()
    if not _confirm_overwrite(pass_key, reset=reset):
        typer.echo("Aborted — the stored token was left in place.")
        raise typer.Exit(code=1)

    secret = typer.prompt("Paste the internal integration secret", hide_input=True).strip()
    _store(pass_key, secret)
    client = _verify(overlay, pass_key, secret)
    unshared = _report_pages(client, list(page or []))
    _report_deployed_containers(pass_key)
    if unshared is not None:
        raise typer.Exit(code=unshared.exit_code)


def _target_pass_key(overlay: str) -> str:
    from teatree.backends.notion.client import NotionTokenCredential  # noqa: PLC0415 — deferred: lazy CLI import
    from teatree.backends.notion.credentials import overlay_notion_pass_key  # noqa: PLC0415 — lazy CLI import
    from teatree.utils.django_bootstrap import ensure_django  # noqa: PLC0415 — deferred: lazy CLI import

    ensure_django()
    _reject_unresolvable(overlay)
    return overlay_notion_pass_key(overlay or None) or NotionTokenCredential.spec.pass_path or ""


def _reject_unresolvable(overlay: str) -> None:
    """A typo must not resolve to "" and store the token under the default key instead."""
    from django.core.exceptions import ImproperlyConfigured  # noqa: PLC0415 — deferred: lazy CLI import

    from teatree.core.overlay_loader import get_overlay  # noqa: PLC0415 — deferred: lazy CLI import

    if not overlay:
        return
    try:
        get_overlay(overlay)
    except ImproperlyConfigured as exc:
        raise fail(exc) from exc


def _announce_integration() -> None:
    typer.echo(f"Step 1/3 — Create an internal integration at {NOTION_INTEGRATIONS_URL}")
    webbrowser.open(NOTION_INTEGRATIONS_URL)
    typer.echo("      Under its Configuration tab, grant ALL of:")
    for capability in CAPABILITIES:
        typer.echo(f"        - {capability}")
    typer.echo("      Capabilities are not page sharing — sharing is the separate per-page grant in step 3.")


def _confirm_overwrite(pass_key: str, *, reset: bool) -> bool:
    if reset:
        return True
    try:
        stored = read_pass(pass_key)
    except SecretStoreError as exc:
        raise fail(exc) from exc
    if not stored:
        return True
    return typer.confirm(f"`pass {pass_key}` already holds a value. Overwrite it?", default=False)


def _store(pass_key: str, secret: str) -> None:
    typer.echo("Step 2/3 — Store the secret, then verify it through the readers' own resolution.")
    try:
        write_pass_with_backup(pass_key, secret, echo=typer.echo)
    except SecretStoreError as exc:
        raise fail(exc) from exc
    typer.echo(f"OK    Stored at `pass {pass_key}`.")


def _verify(overlay: str, pass_key: str, secret: str) -> "NotionClient":
    """Resolve the token back the way every reader does, and name the bot pages must be shared with."""
    from teatree.backends.notion.client import NotionTokenCredential  # noqa: PLC0415 — deferred: lazy CLI import
    from teatree.backends.notion.errors import NotionError  # noqa: PLC0415 — deferred: lazy CLI import

    env_var = NotionTokenCredential.spec.env_var
    if (exported := os.environ.get(env_var, "")) and exported != secret:
        typer.echo(
            f"WARN  {env_var} is exported in this shell and BEATS `pass {pass_key}`, so the check below "
            f"reads the exported value, not the one just stored. Unset it to use the store.",
            err=True,
        )
    try:
        client = notion_client(overlay)
        typer.echo(f"OK    Notion accepted the token — {client.describe_identity()}")
    except NotionError as exc:
        typer.echo("FAIL  The stored token did not resolve back through the path the readers take.", err=True)
        raise fail(exc) from exc
    return client


def _report_pages(client: "NotionClient", pages: list[str]) -> "NotionError | None":
    """Never exit here — the caller still owes the operator the deploy note."""
    typer.echo("Step 3/3 — Check the pages this integration must reach.")
    if not pages:
        typer.echo("      No --page given. An integration sees nothing until each page is shared WITH it.")
        return None
    first_error: NotionError | None = None
    for reference in pages:
        verdict = page_verdict(client, reference)
        status = "OK  " if verdict.ok else "FAIL"
        typer.echo(f"{status}  {reference} — {verdict.headline}", err=not verdict.ok)
        first_error = first_error or verdict.error
    return first_error


def _report_deployed_containers(pass_key: str) -> None:
    typer.echo(f"      Deployed containers read `pass {pass_key}` from this host at deploy time — re-run Deploy")
    typer.echo("      to pick it up. It is never written into deploy/teatree.env, which every deploy rewrites.")
