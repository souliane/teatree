# Rejoins the fork's own rejoin of the two 0057 leaves with upstream's — this fork
# wrote 0058_merge_remove_availability and upstream wrote
# 0058_merge_vendor_sync_and_availability_removal for the same pair, so the two
# 0058s are duplicate rejoins that leave two leaves. Empty: it reconciles the graph
# only. Rejoined rather than renumbered — the fork's chain is already applied in the
# deployed factory database, and renaming an applied migration makes Django re-run it.

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0058_merge_remove_availability"),
        ("core", "0072_owner_pid_namespace_db_default"),
    ]

    operations = []
