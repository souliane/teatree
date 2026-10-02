"""Rejoin the two leaves: the artifact sweep's own stamp and the reclaim-yield streak.

Empty by construction — ``test_live_core_graph_is_linear_by_dependency`` admits several
core parents only on a migration carrying NO operations.
"""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0098_merge_vendor_sync_and_scannedbroadcast"),
        ("core", "0099_carry_the_venv_idle_window_onto_every_artifact"),
    ]

    operations: list = []
