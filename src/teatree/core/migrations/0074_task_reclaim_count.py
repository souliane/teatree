from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0073_merge_vendor_sync_93914a6f1"),
    ]

    operations = [
        migrations.AddField(
            model_name="task",
            name="reclaim_count",
            field=models.PositiveIntegerField(default=0),
        ),
    ]
