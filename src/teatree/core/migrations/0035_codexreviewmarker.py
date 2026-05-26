# Generated for #1254 — auto-dispatch /codex:review per-SHA idempotency ledger.

import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0034_scannedbroadcast_sticky_manual_flag"),
    ]

    operations = [
        migrations.CreateModel(
            name="CodexReviewMarker",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("slug", models.CharField(max_length=128)),
                ("pr_id", models.IntegerField()),
                ("head_sha", models.CharField(max_length=64)),
                ("overlay", models.CharField(blank=True, default="", max_length=64)),
                ("variant", models.CharField(blank=True, default="", max_length=64)),
                ("dispatched_at", models.DateTimeField(default=django.utils.timezone.now)),
            ],
            options={
                "db_table": "teatree_codex_review_marker",
                "ordering": ["-dispatched_at"],
            },
        ),
        migrations.AddConstraint(
            model_name="codexreviewmarker",
            constraint=models.UniqueConstraint(
                fields=("slug", "pr_id", "head_sha"),
                name="uniq_codexreviewmarker_slug_pr_sha",
            ),
        ),
    ]
