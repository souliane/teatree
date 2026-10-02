"""One-time import of an already-authenticated Codex ChatGPT cache."""

import sys
from pathlib import Path

import typer

from teatree.agents.codex_auth_cache import (
    CODEX_AUTH_PASS_ENTRY,
    CodexAuthCacheError,
    store_auth_cache,
    store_auth_cache_from_reader,
)

codex_auth_app = typer.Typer(no_args_is_help=True, help="Manage the private Codex ChatGPT auth cache.")
_MAX_AUTH_BYTES = 1024 * 1024


def _read_auth_source(source: str) -> bytes:
    if source == "-":
        raw = sys.stdin.buffer.read(_MAX_AUTH_BYTES + 1)
    else:
        try:
            with Path(source).expanduser().open("rb") as handle:
                raw = handle.read(_MAX_AUTH_BYTES + 1)
        except OSError:
            message = "could not read the Codex auth cache file"
            raise typer.BadParameter(message, param_hint="--from") from None
    if len(raw) > _MAX_AUTH_BYTES:
        message = "Codex auth cache input is too large (limit: 1 MiB)"
        raise typer.BadParameter(message, param_hint="--from")
    if not raw.strip():
        message = "Codex auth cache input is empty"
        raise typer.BadParameter(message, param_hint="--from")
    return raw


@codex_auth_app.command("import")
def import_auth(
    from_path: str = typer.Option("~/.codex/auth.json", "--from", metavar="PATH|-"),
) -> None:
    """Store a locally authenticated ``auth.json`` as one base64 pass entry."""
    try:
        if from_path == "-":
            store_auth_cache(_read_auth_source(from_path), echo=typer.echo)
        else:
            store_auth_cache_from_reader(lambda: _read_auth_source(from_path), echo=typer.echo)
    except CodexAuthCacheError as exc:
        raise typer.BadParameter(str(exc), param_hint="--from") from exc
    typer.echo(f"OK    Stored Codex auth cache in pass entry {CODEX_AUTH_PASS_ENTRY}.")
