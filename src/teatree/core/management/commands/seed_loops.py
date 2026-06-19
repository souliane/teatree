"""``manage.py seed_loops`` — idempotently seed the default loops + prompts (#2513).

The install-time seed ``t3 setup`` runs (paralleling ``config_setting import
--no-clobber``) so a fresh — or squashed-migration — install has the default
:class:`Loop` rows present. Idempotent: re-running creates nothing new and never
clobbers an operator-edited row. ORM access lives in a management command (the
project's "anything touching the ORM is a management command" rule).
"""

from django_typer.management import TyperCommand

from teatree.loops.seed import seed_default_loops_and_prompts


class Command(TyperCommand):
    help = "Idempotently seed the default loops + prompts (#2513)."

    def handle(self) -> None:
        result = seed_default_loops_and_prompts()
        self.stdout.write(
            f"seeded loops: {result.loops_created} created, prompts: {result.prompts_created} created "
            "(existing rows untouched)."
        )
