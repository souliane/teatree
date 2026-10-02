# Rejoins the two leaves the inbound upstream sync created: the fork's dream
# distill-cursor field (0052_dreamrunmarker_distill_cursor) and upstream's
# resource-pressure/review-cooldown chain (0053_resourcepressuremarker_...).
# Empty, so it only reconciles the graph — every field stays in the migration
# that has always carried it, which is what keeps already-migrated boxes
# consistent.

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0052_dreamrunmarker_distill_cursor"),
        ("core", "0053_resourcepressuremarker_adaptive_intake_concurrency_and_more"),
    ]

    operations = []
