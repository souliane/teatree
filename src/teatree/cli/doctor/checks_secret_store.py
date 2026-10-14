"""Doctor rendering of the database sealing-key verdict."""

from typing import TYPE_CHECKING, cast

import typer
from django.apps import apps
from django.db import DatabaseError

from teatree.utils.django_bootstrap import ensure_django
from teatree.utils.master_key import master_key_source

if TYPE_CHECKING:
    from teatree.core.models.sealed_secret import SealedSecret


def check_secret_store() -> bool:
    ensure_django()
    model = cast("type[SealedSecret]", apps.get_model("core", "SealedSecret"))
    try:
        source = master_key_source()
        health = model.objects.key_health(source, verify_rows=True)
    except (DatabaseError, OSError, ValueError) as exc:
        typer.echo(f"FAIL  Sealed secret key source could not be checked: {type(exc).__name__}")
        return False
    if health.row_count == 0 and not source.path.exists():
        typer.echo(f"WARN  Sealed secret key {health.source}: no key yet; first write will create it")
        return True
    if health.healthy:
        typer.echo(f"OK    Sealed secret key {health.source}: {health.detail}")
        return True
    typer.echo(f"FAIL  Sealed secret key {health.source}: {health.status} — {health.detail}")
    return False
