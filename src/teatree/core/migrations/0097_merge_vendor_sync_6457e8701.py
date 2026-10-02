"""Rejoin the two migration leaves the vendor sync left behind.

Both sides branched off ``0083_alter_task_failure_kind_and_more``: this tree added
``0084_red_mr_fix_attempt_review_findings`` through
``0096_the_sweep_dogfood_and_housekeeping_rows_are_the_cadence``, upstream added
``0084_merge_reclaim_pressure``. Two leaves fail ``django_linear_migrations``
(dlm.E005), and a deployed box would simply never apply the branch that lost
``max_migration.txt``.

Rejoining rather than RENUMBERING is deliberate: this chain is already applied in the
deployed database, and renaming an applied migration makes Django re-run it. A no-op
merge leaves every recorded name intact.

Empty by construction: ``test_live_core_graph_is_linear_by_dependency`` admits several
core parents only on a migration carrying NO operations, because a node that merges AND
migrates is the renumber-at-merge accident.
"""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0084_merge_reclaim_pressure"),
        ("core", "0096_the_sweep_dogfood_and_housekeeping_rows_are_the_cadence"),
    ]

    operations: list = []
