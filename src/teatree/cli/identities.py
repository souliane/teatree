"""``t3 identities {seed,add,list,remove}`` — manage the trusted-identity set (#1773).

A thin Typer group that dispatches to the teatree-CORE ``identities``
management command via :func:`managepy_core` (``python -m teatree``). The
trusted-identity set is workspace-global (the user's handles across forges),
not per-overlay, so it lives at the top level rather than under an overlay.
"""

import typer

from teatree.cli.overlay import managepy_core

identities_app = typer.Typer(no_args_is_help=True, help="Manage the user's trusted forge identities (#1773).")


@identities_app.command()
def seed() -> None:
    """Consolidate the configured ``user_identity_aliases`` into the DB (idempotent)."""
    managepy_core("identities", "seed")


@identities_app.command()
def add(
    platform: str = typer.Argument(..., help="github | gitlab | slack | internal"),
    handle: str = typer.Argument(..., help="The forge handle / login to trust."),
    note: str = typer.Option("", "--note", help="Free-form upkeep note."),
) -> None:
    """Add a trusted identity (idempotent on ``(platform, handle)``)."""
    extra = ("--note", note) if note else ()
    managepy_core("identities", "add", platform, handle, *extra)


@identities_app.command(name="list")
def list_() -> None:
    """List all trusted identities."""
    managepy_core("identities", "list")


@identities_app.command()
def remove(
    platform: str = typer.Argument(..., help="github | gitlab | slack | internal"),
    handle: str = typer.Argument(..., help="The forge handle / login to untrust."),
) -> None:
    """Remove a trusted identity by ``(platform, handle)``."""
    managepy_core("identities", "remove", platform, handle)
