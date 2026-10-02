from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0076_botping_pulled_status"),
    ]

    operations = [
        migrations.AddField(
            model_name="pendingchatinjection",
            name="thread_ts",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
    ]
