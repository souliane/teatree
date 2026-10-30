"""Widen ``RedMrFixAttempt.head_sha`` to 82 characters instead of adding the planned ``activity_key`` column.

A review-findings claim is keyed on the newest human note id as ``~<16 hex note id>:<64 hex sha>``
(82 characters), and a ``<review-activity>`` cursor row shares the column, so the existing unique key
``(pr_url, head_sha, kind)`` keeps deduplicating without a second nullable ``activity_key`` column
(the plan numbered that column 0034).
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0035_merge_operator_grant_and_config_setting_change"),
    ]

    operations = [
        migrations.AlterField(
            model_name="redmrfixattempt",
            name="head_sha",
            field=models.CharField(max_length=82),
        ),
    ]
