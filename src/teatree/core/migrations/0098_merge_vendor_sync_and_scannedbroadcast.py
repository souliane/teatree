"""Rejoin the two 0097 leaves: the vendor-sync merge and the ``ScannedBroadcast.taken`` choice.

Empty by construction — ``test_live_core_graph_is_linear_by_dependency`` admits several
core parents only on a migration carrying NO operations.
"""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0097_merge_vendor_sync_6457e8701"),
        ("core", "0097_scannedbroadcast_taken"),
    ]

    operations: list = []
