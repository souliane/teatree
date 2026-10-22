from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0016_cievalhealsession_rerun_and_artifact_wait"),
    ]

    operations = [
        migrations.AddField(
            model_name="cievalhealsession",
            name="park_cycles",
            field=models.PositiveSmallIntegerField(default=0),
        ),
    ]
