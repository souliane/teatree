from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0070_merge_20260611_0932"),
    ]

    operations = [
        migrations.AddField(
            model_name="task",
            name="claimed_by_session",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
    ]
