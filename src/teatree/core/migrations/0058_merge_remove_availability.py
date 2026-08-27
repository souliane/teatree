# Rejoins the two 0057 leaves off 0056_task_admitted_at — this fork's vendor-sync
# rejoin and upstream's availability-mode removal. Empty: it reconciles the graph only.

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0057_merge_upstream_vendor_sync"),
        ("core", "0057_remove_mode_availability_mode"),
    ]

    operations = []
