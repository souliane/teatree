"""``taken`` — somebody else already holds the review, so the broadcast never dispatches (#159)."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0096_the_sweep_dogfood_and_housekeeping_rows_are_the_cadence")]

    operations = [
        migrations.AlterField(
            model_name="scannedbroadcast",
            name="classification",
            field=models.CharField(
                choices=[("all_merged", "All Merged"), ("pending", "Pending"), ("taken", "Taken")],
                max_length=16,
            ),
        ),
    ]
