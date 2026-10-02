from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0100_merge_the_artifact_sweep_and_the_reclaim_yield"),
    ]

    operations = [
        migrations.AddField(
            model_name="reviewrequestpost",
            name="last_nag_reply_ts",
            field=models.CharField(blank=True, max_length=64),
        ),
    ]
