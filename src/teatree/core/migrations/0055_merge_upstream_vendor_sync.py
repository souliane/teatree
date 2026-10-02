# Rejoins the two leaves the inbound upstream sync created off
# 0053_resourcepressuremarker_...: this fork's own rejoin
# (0054_merge_upstream_vendor_sync) and upstream's failure-reason backfill
# (0054_backfill_task_failure_reason). Empty, so it only reconciles the graph —
# every field and every data migration stays in the migration that has always
# carried it, which is what keeps already-migrated boxes consistent.

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0054_merge_upstream_vendor_sync"),
        ("core", "0054_backfill_task_failure_reason"),
    ]

    operations = []
