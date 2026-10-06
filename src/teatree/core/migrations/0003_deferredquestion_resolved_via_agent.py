from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0002_drop_self_pump_driver"),
    ]

    operations = [
        migrations.AlterField(
            model_name="deferredquestion",
            name="resolved_via",
            field=models.CharField(
                blank=True,
                choices=[
                    ("", "Unresolved"),
                    ("slack", "Slack reply"),
                    ("local", "Local CLI"),
                    ("stale", "Stale"),
                    ("policy", "Policy auto-answer"),
                    ("agent", "Agent surface"),
                ],
                default="",
                max_length=8,
            ),
        ),
    ]
