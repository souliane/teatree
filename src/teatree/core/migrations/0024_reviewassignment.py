# Generated for #1047 — reaction-driven review auto-assign + emoji feedback loop.

import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0023_looplease_session_owner"),
    ]

    operations = [
        migrations.CreateModel(
            name="ReviewAssignment",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("overlay", models.CharField(blank=True, default="", max_length=64)),
                ("mr_url", models.URLField(max_length=500)),
                ("user_id", models.CharField(max_length=64)),
                ("channel", models.CharField(max_length=64)),
                ("slack_ts", models.CharField(max_length=64)),
                (
                    "state",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("eyes_added", "Eyes Added"),
                            ("approved", "Approved"),
                        ],
                        default="pending",
                        max_length=16,
                    ),
                ),
                ("trigger", models.CharField(default="", max_length=16)),
                ("observed_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("approved_at", models.DateTimeField(blank=True, null=True)),
            ],
            options={
                "db_table": "teatree_review_assignment",
                "ordering": ["observed_at"],
            },
        ),
        migrations.AddConstraint(
            model_name="reviewassignment",
            constraint=models.UniqueConstraint(
                fields=("overlay", "mr_url", "user_id"),
                name="uniq_reviewassignment_overlay_mr_user",
            ),
        ),
    ]
