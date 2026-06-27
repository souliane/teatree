"""Drop the MiniLoopMarker table — the duplicate cadence ledger is gone (LOOP-PR-A).

The #1432 ``MiniLoopMarker`` cadence ledger and its orchestrator are deleted; the
single cadence ledger is ``Loop.last_run_at`` (#2513). The dream cron, the last
reader/writer of the marker, now gates on ``Loop.is_due`` / ``mark_run``.
"""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0003_instructioncompliancesnapshot_and_more"),
    ]

    operations = [
        migrations.DeleteModel(name="MiniLoopMarker"),
    ]
