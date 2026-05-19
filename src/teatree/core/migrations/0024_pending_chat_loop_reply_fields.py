# Generated for #1075 — the reactive Slack-answer loop's own columns.
#
# Option B: the loop gets ``loop_replied_at`` (a column distinct from
# #1069's ``answered_at`` turn-end gate, added by 0022), so a token-cheap
# loop reply never silently satisfies the #1063 Stop-hook "agent
# personally replied" gate. This migration adds ONLY the loop's net-new
# columns; ``answered_at`` already exists from
# ``0022_pendingchatinjection_answered_at`` and is intentionally NOT
# re-added here.

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0023_looplease_session_owner"),
    ]

    operations = [
        migrations.AddField(
            model_name="pendingchatinjection",
            name="loop_replied_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="pendingchatinjection",
            name="answer_kind",
            field=models.CharField(
                blank=True,
                choices=[("", "Unanswered"), ("ack", "Ack"), ("simple", "Simple"), ("delegated", "Delegated")],
                default="",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="pendingchatinjection",
            name="eyes_reacted_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
