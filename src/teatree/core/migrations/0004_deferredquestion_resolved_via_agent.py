from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0003_delete_drain_slot_reservation_rows"),
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
