# Generated for #1391 — per-article ask gate for the scanning-news scanner.

import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0036_codexreviewmarker"),
    ]

    operations = [
        migrations.CreateModel(
            name="PendingArticleSuggestion",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("url", models.URLField(max_length=2048)),
                ("url_hash", models.CharField(max_length=64, unique=True)),
                ("title", models.TextField(blank=True, default="")),
                ("summary", models.TextField()),
                ("source", models.CharField(blank=True, default="", max_length=64)),
                ("presented", models.BooleanField(default=False)),
                ("presented_at", models.DateTimeField(blank=True, null=True)),
                (
                    "decision",
                    models.CharField(
                        choices=[
                            ("pending", "pending"),
                            ("approved", "approved"),
                            ("rejected", "rejected"),
                        ],
                        default="pending",
                        max_length=16,
                    ),
                ),
                ("decided_at", models.DateTimeField(blank=True, null=True)),
                ("decider_id", models.CharField(blank=True, default="", max_length=255)),
                ("decision_reason", models.TextField(blank=True, default="")),
                ("created_ticket_url", models.URLField(blank=True, default="", max_length=2048)),
            ],
            options={
                "db_table": "teatree_pending_article_suggestion",
                "ordering": ["-created_at"],
            },
        ),
    ]
