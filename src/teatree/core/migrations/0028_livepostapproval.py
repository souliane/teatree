"""Live-post approval ledger for the ``--live`` post-comment pre-gate (#1207)."""

import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0027_canonicalize_teatree_overlay"),
    ]

    operations = [
        migrations.CreateModel(
            name="LivePostApproval",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("mr_url", models.CharField(max_length=512)),
                ("slack_ts", models.CharField(max_length=64)),
                ("slack_user_id", models.CharField(max_length=64)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("consumed_at", models.DateTimeField(blank=True, null=True)),
            ],
            options={
                "db_table": "teatree_live_post_approval",
                "ordering": ["-created_at"],
            },
        ),
    ]
