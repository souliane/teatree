"""Add ``Loop.colleague_facing`` and seed it on the default colleague-facing loops.

The column is a genuinely new field — no pre-existing row could carry an
operator-set value for it — so the backfill below is establishing the field's
first value, not overwriting an operator choice (unlike the blank-sentinel
``description`` backfill in ``0009_seed_loop_descriptions``). A migration is
frozen history and must not import the evolving ``teatree.loops.seed`` module,
so the colleague-facing set is INLINED here;
``tests/teatree_core/test_loop_colleague_facing_migration.py`` pins this
inlined set against ``teatree.loops.seed.DEFAULT_LOOPS`` so the migrate-path
and the install-seed cannot drift.
"""

from django.db import migrations, models

# Loops that reach/read a colleague (review comments, the review-request nag).
_COLLEAGUE_FACING_LOOPS = frozenset({"review", "followup"})


def _seed_colleague_facing(apps, schema_editor):
    Loop = apps.get_model("core", "Loop")
    Loop.objects.filter(name__in=_COLLEAGUE_FACING_LOOPS).update(colleague_facing=True)


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0015_agent_harness_two_layer_config"),
    ]

    operations = [
        migrations.AddField(
            model_name="loop",
            name="colleague_facing",
            field=models.BooleanField(default=False),
        ),
        migrations.RunPython(_seed_colleague_facing, migrations.RunPython.noop),
    ]
