"""Rejoin the two migration leaves the 93914a6f1..2c09c9f80 vendor sync left behind.

Both sides branched off the recorded base: the fork added
``0075_task_failure_kind_credential_missing``, upstream added
``0076_botping_pulled_status`` through ``0081_plan_missing_failure_kind``. Two leaves
fail ``django_linear_migrations`` (dlm.E005), and a deployed box would simply never
apply the branch that lost ``max_migration.txt``.

Rejoining rather than RENUMBERING is deliberate: the fork's chain is already applied
in the deployed factory database, and renaming an applied migration makes Django
re-run it. A no-op merge leaves every recorded name intact.

Empty by construction. Both leaves alter ``failure_kind``'s choices, so restating the
merged vocabulary here is tempting — and refused: ``test_live_core_graph_is_linear_by_dependency``
allows several core parents only on a migration carrying NO operations, because a node
that merges AND migrates is the renumber-at-merge accident. Choices are not a database
constraint on SQLite or PostgreSQL, so the merged vocabulary needs no schema operation;
``makemigrations --check`` reads it off the model.
"""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0075_task_failure_kind_credential_missing"),
        ("core", "0081_plan_missing_failure_kind"),
    ]

    operations: list = []
