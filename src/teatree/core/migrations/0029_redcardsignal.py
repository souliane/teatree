# Generated for #1130 — RED CARD detection scanner ledger.

import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0028_scannedbroadcast"),
    ]

    operations = [
        migrations.CreateModel(
            name="RedCardSignal",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("overlay", models.CharField(blank=True, default="", max_length=64)),
                ("channel", models.CharField(max_length=64)),
                ("slack_ts", models.CharField(max_length=64)),
                (
                    "signal_kind",
                    models.CharField(
                        choices=[
                            ("red_circle", "Red Circle reaction"),
                            ("no_entry_sign", "No Entry Sign reaction"),
                            ("red_card_text", "Literal 'RED CARD' text"),
                        ],
                        max_length=32,
                    ),
                ),
                ("user_id", models.CharField(max_length=64)),
                ("offending_message_ts", models.CharField(blank=True, default="", max_length=64)),
                ("offending_message_text", models.TextField(blank=True, default="")),
                ("signal_text", models.TextField(blank=True, default="")),
                (
                    "state",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("eyes_added", "Eyes Added"),
                            ("issue_filed", "Issue Filed"),
                            ("resolved", "Resolved"),
                        ],
                        default="pending",
                        max_length=16,
                    ),
                ),
                ("observed_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("eyes_reacted_at", models.DateTimeField(blank=True, null=True)),
                ("filed_issue_url", models.URLField(blank=True, default="", max_length=500)),
                ("issue_filed_at", models.DateTimeField(blank=True, null=True)),
                ("resolved_at", models.DateTimeField(blank=True, null=True)),
            ],
            options={
                "db_table": "teatree_red_card_signal",
                "ordering": ["observed_at"],
            },
        ),
        migrations.AddConstraint(
            model_name="redcardsignal",
            constraint=models.UniqueConstraint(
                fields=("overlay", "channel", "slack_ts"),
                name="uniq_redcardsignal_overlay_channel_ts",
            ),
        ),
    ]
