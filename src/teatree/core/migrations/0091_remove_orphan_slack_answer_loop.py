"""Remove the orphan ``slack_answer`` ``Loop`` row (#2584).

``slack_answer`` was seeded as an autonomous ``Loop`` row by migration 0078, but
it has no registry ``MiniLoop`` (no ``teatree.loops.slack_answer.loop`` package),
so the autonomous fan-out (``build_loop_table_jobs`` / ``iter_loops``) can never
run it. It runs ONLY via the won-tick piggyback cycle
(``teatree.loop.tick_piggyback.run_piggyback_cycles`` →
``teatree.loop.slack_answer.cycle.run_slack_answer_cycle``), behind its own
``loop-slack-answer`` lease — never off the ``Loop`` table.

The #2513 cutover left this orphan row in the table, so the seeded ``Loop``-table
count (19) and ``iter_loops()`` count (18) disagreed. This migration deletes the
orphan so a fresh-migrate DB matches the install-time seed
(``teatree.loops.seed.DEFAULT_LOOPS``, which #2584 drops ``slack_answer`` from)
and ``iter_loops()``. Reverse re-seeds the (paused) row so the migration is
honestly reversible without re-enabling the autonomous fan-out attempt.
"""

from django.db import migrations

_ORPHAN_NAME = "slack_answer"


def _remove_orphan(apps, schema_editor) -> None:
    loop_model = apps.get_model("core", "Loop")
    loop_model.objects.filter(name=_ORPHAN_NAME).delete()


def _restore_orphan(apps, schema_editor) -> None:
    loop_model = apps.get_model("core", "Loop")
    loop_model.objects.get_or_create(
        name=_ORPHAN_NAME,
        defaults={
            "delay_seconds": 20,
            "daily_at": None,
            "enabled": False,
            "script": "src/teatree/loops/run.py",
        },
    )


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0090_landscapeartifact"),
    ]

    operations = [
        migrations.RunPython(_remove_orphan, _restore_orphan),
    ]
