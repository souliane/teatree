"""Rejoin the leaf #4644 added off 0081 with the one main advanced to 0083.

Empty by construction: ``test_live_core_graph_is_linear_by_dependency`` admits several
core parents only on a migration carrying no operations.
"""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0082_resource_pressure_zero_yield"),
        ("core", "0083_alter_task_failure_kind_and_more"),
    ]

    operations: list = []
