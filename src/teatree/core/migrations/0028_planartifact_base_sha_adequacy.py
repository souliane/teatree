from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0027_standinggoal"),
    ]

    operations = [
        migrations.AddField(
            model_name="planartifact",
            name="base_sha",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
        migrations.AddField(
            model_name="planartifact",
            name="adequacy",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
