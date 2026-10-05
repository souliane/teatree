from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0117_ticket_sweep_run")]

    operations = [
        migrations.AddField(
            model_name="loop",
            name="consecutive_deadline_kills",
            field=models.PositiveIntegerField(default=0),
        ),
    ]
