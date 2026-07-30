# #3157 E5: flag whether TaskAttempt.cost_usd is a price-table estimate vs a reported figure.
#
# The core migration numbering intentionally SKIPS 0006. This migration and the
# sibling 0008_loop_presets were authored in parallel off 0005 (both born 0006) and
# renumbered at merge time to keep the graph a single linear chain
# (0005 -> 0007 -> 0008) instead of a 0006 fork. The gap is harmless: Django keys
# applied state on migration NAMES and DEPENDENCIES, never on contiguous numbers.
# The linear-by-dependency invariant is pinned by
# tests/teatree_core/test_linear_migrations.py.

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0005_taskattempt_outcome"),
    ]

    operations = [
        migrations.AddField(
            model_name="taskattempt",
            name="cost_is_estimated",
            field=models.BooleanField(default=True),
        ),
    ]
