"""Rejoin the two migration leaves the 17e98927b..6c79893e7 vendor sync left behind.

Both branches descend from ``0034_taskattempt_park_repeats``: the fork's runs
``0036_merge_upstream_vendor_sync`` -> ``0037_review_backend_cooldown``, upstream's runs
``0035_housekeeping_description_board_reconcile`` -> ``0036_merge_squashed_migration_leaf``
-> ``0037_taskattempt_taskattempt_cost_cover`` -> ``0038_ticket_transition_indexes``. Two
leaves fail ``django_linear_migrations`` (dlm.E005), and the sync's ``max_migration.txt``
names upstream's leaf alone — which would strand the fork's branch unapplied with no error.

Rejoining rather than RENUMBERING either branch is deliberate: the fork's chain is already
applied in the deployed factory database, and renaming an applied migration makes Django
re-run it (``CreateModel`` onto a table that already exists). A no-op merge leaves every
recorded name intact, so a deployed box has only upstream's four and this rejoin left to
apply.
"""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0037_review_backend_cooldown"),
        ("core", "0038_ticket_transition_indexes"),
    ]

    operations: list = []
