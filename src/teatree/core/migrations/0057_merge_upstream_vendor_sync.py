# Rejoins the two leaves the inbound upstream sync created off
# 0054_backfill_task_failure_reason: this fork's own rejoin
# (0055_merge_upstream_vendor_sync) and upstream's branch-update/admission chain
# (0055_branchupdateattempt -> 0056_task_admitted_at). Empty, so it only
# reconciles the graph — every field and every data migration stays in the
# migration that has always carried it, which is what keeps already-migrated
# boxes consistent.

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0055_merge_upstream_vendor_sync"),
        ("core", "0056_task_admitted_at"),
    ]

    operations = []
