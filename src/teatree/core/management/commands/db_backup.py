"""``manage.py db_backup`` — snapshot the control DB + prune keep-last-N-days retention (directive #2).

The manual/CLI entry point onto the shared backup engine
(:mod:`teatree.core.db_backup`) — the SAME engine the ``db_backup`` mini-loop's
mechanical handler drives, so a hand-run backup and the daily loop can never
diverge. ``--retention-days`` defaults to the configured ``db_backup_retention_days``
(``[teatree]`` config, per-overlay overridable); pass it to override for a one-off
run. Anything touching the control DB lives in a management command (the project's
"anything touching the ORM/DB is a management command" rule).
"""

from typing import Annotated

import typer
from django_typer.management import TyperCommand

from teatree.config import get_effective_settings
from teatree.core.db_backup import run_backup


class Command(TyperCommand):
    help = "Back up teatree's control DB and prune past the keep-last-N-days retention (directive #2)."

    def handle(
        self,
        retention_days: Annotated[
            int,
            typer.Option(
                help="Days of backups to keep (default: the configured db_backup_retention_days). "
                "A non-positive value falls back to the configured retention.",
            ),
        ] = 0,
    ) -> None:
        """Take one control-DB backup and prune retention-expired artifacts."""
        effective = retention_days if retention_days > 0 else get_effective_settings().db_backup_retention_days
        result = run_backup(retention_days=effective)
        if result.created is not None:
            self.stdout.write(f"backup written: {result.created}")
        else:
            self.stdout.write(f"no backup written ({result.skipped_reason})")
        self.stdout.write(f"pruned {len(result.pruned)} backup(s) past the {effective}-day retention.")
