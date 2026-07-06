"""Seed the ``directive_loop`` default loop row PAUSED on an already-migrated DB (north-star PR-7).

The ``directive_loop`` MiniLoop is new: an install that already ran ``0001_initial``
before it existed has no row for it, so the loop-table fan-out could never admit it
and the seed/registry parity is broken on the upgrade path. This idempotently
``get_or_create``s the row — ``enabled=False`` (QUADRUPLE-OFF layer 2: the loop stays
dark until an operator deliberately enables it), script-backed at its own module, with
its canonical description and ``colleague_facing=False``.

A migration is frozen history and must not import the evolving ``teatree.loops.seed``
module, so the values are INLINED; a fresh migrate seeds the same row via
``0001_initial._seed_default_loops`` (the parity tests pin both against the canonical
seed), and this ``get_or_create`` is then a no-op — so the migrate-path is consistent
whether the DB is fresh or upgraded, and an operator-edited row is never clobbered.
"""

from django.db import migrations

_DIRECTIVE_LOOP_NAME = "directive_loop"
_DIRECTIVE_LOOP_DELAY_SECONDS = 86400
_DIRECTIVE_LOOP_DESCRIPTION = (
    "Advances one ratified directive one step per day (implement, configure, verify, "
    "keep-only-if-verified, else human-asked revert), off the live tick; ships disabled "
    "behind the directive_loop_enabled flag and the critic-live guard."
)


def _seed_directive_loop(apps, schema_editor):
    Loop = apps.get_model("core", "Loop")
    Loop.objects.get_or_create(
        name=_DIRECTIVE_LOOP_NAME,
        defaults={
            "delay_seconds": _DIRECTIVE_LOOP_DELAY_SECONDS,
            "daily_at": None,
            "enabled": False,
            "colleague_facing": False,
            "description": _DIRECTIVE_LOOP_DESCRIPTION,
            "script": f"src/teatree/loops/{_DIRECTIVE_LOOP_NAME}/loop.py",
        },
    )


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0034_directive"),
    ]

    operations = [
        migrations.RunPython(_seed_directive_loop, migrations.RunPython.noop),
    ]
