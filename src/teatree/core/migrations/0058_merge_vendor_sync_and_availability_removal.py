# Rejoins the two leaves that met when main merged into this branch: this
# fork's vendor-sync rejoin (0057_merge_upstream_vendor_sync) and upstream's
# removal of the dead Mode.availability_mode column
# (0057_remove_mode_availability_mode). Both hang off 0056_task_admitted_at and
# touch disjoint state, so the rejoin is empty — every field and every data
# migration stays in the migration that has always carried it, which is what
# keeps already-migrated boxes consistent.

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0057_merge_upstream_vendor_sync"),
        ("core", "0057_remove_mode_availability_mode"),
    ]

    operations = []
