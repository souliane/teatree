# Generated for #1131 — Slack review-broadcast scanner idempotency ledger.

import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0027_canonicalize_teatree_overlay"),
    ]

    operations = [
        migrations.CreateModel(
            name="ScannedBroadcast",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("overlay", models.CharField(blank=True, default="", max_length=64)),
                ("channel", models.CharField(max_length=64)),
                ("slack_ts", models.CharField(max_length=64)),
                ("mr_urls", models.JSONField(default=list)),
                (
                    "classification",
                    models.CharField(
                        choices=[
                            ("all_merged", "All Merged"),
                            ("pending", "Pending"),
                        ],
                        max_length=16,
                    ),
                ),
                ("reviewer_task_id", models.CharField(blank=True, default="", max_length=64)),
                ("observed_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("reclassified_at", models.DateTimeField(blank=True, null=True)),
            ],
            options={
                "db_table": "teatree_scanned_broadcast",
                "ordering": ["observed_at"],
            },
        ),
        migrations.AddConstraint(
            model_name="scannedbroadcast",
            constraint=models.UniqueConstraint(
                fields=("channel", "slack_ts"),
                name="uniq_scannedbroadcast_channel_ts",
            ),
        ),
    ]
