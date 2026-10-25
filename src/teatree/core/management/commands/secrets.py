"""Inspect sealed rows and plan a read-only password-store import."""

from typing import Annotated

import typer
from django_typer.management import TyperCommand, command, initialize

from teatree.core.models.sealed_secret import SealedSecret
from teatree.core.sealed.pass_import import build_import_plan
from teatree.utils.master_key import master_key_source


class Command(TyperCommand):
    @initialize()
    def init(self) -> None:
        pass

    @command()
    def status(self) -> None:
        """Report key health without disclosing stored values."""
        source = master_key_source()
        health = SealedSecret.objects.key_health(source)
        typer.echo(f"source {source.label}")
        typer.echo(f"health {health.status}: {health.detail}")
        if health.row_count:
            for key_id, count in sorted(SealedSecret.objects.key_census().items()):
                typer.echo(f"key_id {key_id}: {count} rows")
        if not health.healthy:
            raise SystemExit(1)

    @command()
    def ls(self, prefix: Annotated[str, typer.Argument(help="Only names starting with this prefix.")] = "") -> None:
        """List sealed row names without decrypting values."""
        for name in SealedSecret.objects.names_starting(prefix):
            typer.echo(name)

    @command(name="import-pass")
    def import_pass(self) -> None:
        """Classify password-store names without copying their values."""
        for entry in build_import_plan():
            typer.echo(f"{entry.destination}\t{entry.readability}\t{entry.name}")
