"""Rejoin the fire-anchor branch onto the chain main advanced while it sat open.

Empty by construction — ``test_live_core_graph_is_linear_by_dependency`` admits several
core parents only on a migration carrying NO operations. Renumbering
``0097_a_lease_release_keeps_the_fire_anchor`` onto the new leaf would re-run it on any
box that already applied it.
"""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0097_a_lease_release_keeps_the_fire_anchor"),
        ("core", "0110_merge_clear_merged_without_squash"),
    ]

    operations: list = []
