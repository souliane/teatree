"""An unread rate-limit window becomes NULL, so no reader mistakes it for full headroom.

The reactive exhaustion writer knows only the window that refused; the other one it never
read was coerced to ``0.0``, which every later reader — the operator report, the dash, the
admission brake — could not tell from a measured idle window.

Reverses plan decisions D2/D5, which held that re-probing kept fabricated values out of the
cache and so needed no column change: that premise holds only when the probe SUCCEEDS, and
fails on exactly the probe-failure path this column exists to record.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0105_task_session_continuation")]

    operations = [
        migrations.AlterField(
            model_name="anthropictokenusage",
            name="utilization_5h",
            field=models.FloatField(blank=True, default=None, null=True),
        ),
        migrations.AlterField(
            model_name="anthropictokenusage",
            name="utilization_7d",
            field=models.FloatField(blank=True, default=None, null=True),
        ),
    ]
