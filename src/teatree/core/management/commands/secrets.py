"""Inspect sealed rows and plan a read-only password-store import."""

import typer
from django_typer.management import TyperCommand, command, initialize

from teatree.core.models.sealed_secret import SealedSecret
from teatree.core.secret_import import build_import_plan
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
        for key_id, count in sorted(SealedSecret.objects.key_census().items()):
            typer.echo(f"key_id {key_id}: {count} rows")

    @command()
    def ls(self, prefix: str = "") -> None:
        """List sealed row names without decrypting values."""
        names = SealedSecret.objects.values_list("name", flat=True)
        for name in sorted(name for name in names if name.startswith(prefix)):
            typer.echo(name)

    @command(name="import-pass")
    def import_pass(self) -> None:
        """Classify password-store names without copying their values."""
        for entry in build_import_plan():
            typer.echo(f"{entry.destination}\t{entry.readability}\t{entry.name}")
