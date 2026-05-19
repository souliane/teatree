# Generated for #1014 — Slack ↔ Claude-Code bidirectional bridge (BLUEPRINT §17.1 invariant 2).

import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0018_deferred_question"),
    ]

    operations = [
        migrations.CreateModel(
            name="PendingChatInjection",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("overlay", models.CharField(blank=True, default="", max_length=64)),
                ("channel", models.CharField(max_length=64)),
                ("slack_ts", models.CharField(max_length=64)),
                ("user_id", models.CharField(blank=True, default="", max_length=64)),
                ("text", models.TextField()),
                ("received_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("consumed_at", models.DateTimeField(blank=True, null=True)),
            ],
            options={
                "db_table": "teatree_pending_chat_injection",
                "ordering": ["received_at"],
            },
        ),
        migrations.AddConstraint(
            model_name="pendingchatinjection",
            constraint=models.UniqueConstraint(fields=("overlay", "slack_ts"), name="uniq_pendingchat_overlay_ts"),
        ),
    ]
