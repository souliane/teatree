"""Require script loops to carry an interval.

The model-level clean() invariant already rejects ``script`` loops with a null
``delay_seconds``. Backfill any pre-existing invalid rows to a conservative
60-second interval, then enforce the same rule at the database layer.
"""

from django.db import migrations, models

_SCRIPT_LOOP_DEFAULT_DELAY_SECONDS = 60


def _backfill_script_loop_delay(apps, schema_editor) -> None:
    Loop = apps.get_model("core", "Loop")
    Loop.objects.filter(script__gt="", delay_seconds__isnull=True).update(
        delay_seconds=_SCRIPT_LOOP_DEFAULT_DELAY_SECONDS,
    )


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0082_consolidatedmemory_disposition_and_more"),
    ]

    operations = [
        migrations.RunPython(_backfill_script_loop_delay, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="loop",
            constraint=models.CheckConstraint(
                condition=models.Q(script="") | models.Q(delay_seconds__isnull=False),
                name="loop_script_requires_delay",
            ),
        ),
    ]
