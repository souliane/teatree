# Generated for #1320 — sticky ``manually_classified`` flag on ScannedBroadcast.
#
# Operator-applied skip signals (my_notes, non-self reactions, author=me,
# upvotes) flip a row to ``all_merged`` via ``mark_manually_classified``;
# the flag survives subsequent rescans so ``_classify()`` does not revert
# the verdict on the next ``t3 loop tick``.

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0033_autonomous_review_team_ledgers"),
    ]

    operations = [
        migrations.AddField(
            model_name="scannedbroadcast",
            name="manually_classified",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="scannedbroadcast",
            name="manually_classified_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
