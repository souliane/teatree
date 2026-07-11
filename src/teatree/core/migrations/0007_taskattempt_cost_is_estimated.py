# #3157 E5: flag whether TaskAttempt.cost_usd is a price-table estimate vs a reported figure.

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0005_taskattempt_outcome"),
    ]

    operations = [
        migrations.AddField(
            model_name="taskattempt",
            name="cost_is_estimated",
            field=models.BooleanField(default=True),
        ),
    ]
