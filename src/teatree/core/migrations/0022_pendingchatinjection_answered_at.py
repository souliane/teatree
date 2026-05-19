# Generated for #1063 — turn-end gate for unanswered user questions.
#
# Adds ``answered_at`` to :class:`PendingChatInjection`. ``consumed_at``
# only proves the row was read into ``additionalContext``; ``answered_at``
# proves the agent actually replied. Stop hook + heuristic in #1063 use
# this column to decide whether to soft-block the turn end.
#
# Existing rows are old (months of empirical drift) and intentionally
# NOT backfilled — they are considered effectively closed out.

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0021_outbound_claim"),
    ]

    operations = [
        migrations.AddField(
            model_name="pendingchatinjection",
            name="answered_at",
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
    ]
