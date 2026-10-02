"""Add the ``review_findings`` slot to the red-MR fix ledger.

A green, conflict-free head can still carry a review finding nobody implemented.
That is a third independent un-mergeable condition with its own remedy, so it gets
its own ledger slot rather than sharing (and being deduped against) the CI-red one.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0083_alter_task_failure_kind_and_more"),
    ]

    operations = [
        migrations.AlterField(
            model_name="redmrfixattempt",
            name="kind",
            field=models.CharField(
                choices=[
                    ("ci_red", "CI red"),
                    ("merge_conflict", "Merge conflict"),
                    ("review_findings", "Review findings"),
                ],
                default="ci_red",
                max_length=32,
            ),
        ),
    ]
