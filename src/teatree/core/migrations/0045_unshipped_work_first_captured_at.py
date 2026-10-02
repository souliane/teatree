"""Give a captured checkout an age that a re-capture cannot reset (#3891).

``captured_at`` is ``auto_now``, so every sweep that re-captures a kept checkout
pushed it back to now — an age report built on it reads zero forever on any host
that sweeps regularly. ``first_captured_at`` is set on insert only.

Existing rows are backfilled from ``captured_at`` rather than from now: it is the
only evidence of when the row was written, so using it keeps a long-held checkout
looking long-held instead of resetting the whole ledger on deploy. The backfill
runs as a queryset UPDATE because ``auto_now_add`` ignores an assigned value.
"""

import django.utils.timezone
from django.db import migrations, models


def _backfill_from_last_capture(apps, schema_editor):
    model = apps.get_model("core", "UnshippedWorkRecord")
    for row in model.objects.all().iterator():
        model.objects.filter(pk=row.pk).update(first_captured_at=row.captured_at)


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0044_retire_dead_review_state"),
    ]

    operations = [
        migrations.AddField(
            model_name="unshippedworkrecord",
            name="first_captured_at",
            field=models.DateTimeField(auto_now_add=True, default=django.utils.timezone.now),
            preserve_default=False,
        ),
        migrations.RunPython(_backfill_from_last_capture, migrations.RunPython.noop),
    ]
