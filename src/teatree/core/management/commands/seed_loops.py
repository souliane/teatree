"""``manage.py seed_loops`` — idempotently seed the default loops + prompts (#2513).

The install-time seed ``t3 setup`` runs (paralleling ``config_setting import
--no-clobber``) so a fresh — or squashed-migration — install has the default
:class:`Loop` rows present. Idempotent: re-running creates nothing new and never
clobbers an operator-edited row. ORM access lives in a management command (the
project's "anything touching the ORM is a management command" rule).
"""

from typing import Annotated

import typer
from django_typer.management import TyperCommand

from teatree.loops.preset_seed import seed_default_presets_and_schedules
from teatree.loops.seed import seed_default_loops_and_prompts
from teatree.loops.seed_drift import classification_drift, reconcile_classification


class Command(TyperCommand):
    help = "Idempotently seed the default loops + prompts + presets + schedules (#2513, #3159)."

    def handle(
        self,
        *,
        reconcile_classification_: Annotated[
            bool,
            typer.Option(
                "--reconcile-classification",
                help="Write the shipped colleague_facing back onto rows that drifted from defaults.toml.",
            ),
        ] = False,
    ) -> None:
        result = seed_default_loops_and_prompts()
        presets = seed_default_presets_and_schedules()
        self.stdout.write(
            f"seeded loops: {result.loops_created} created, prompts: {result.prompts_created} created, "
            f"presets: {presets.presets_created} created, schedules: {presets.schedules_created} created "
            "(existing rows untouched)."
        )
        self._report_classification(reconcile=reconcile_classification_)

    def _report_classification(self, *, reconcile: bool) -> None:
        if reconcile:
            for line in reconcile_classification():
                self.stdout.write(f"reconciled {line}")
            return
        for finding in classification_drift():
            self.stdout.write(f"DRIFT {finding} — run `seed_loops --reconcile-classification` to write it back")
