from django.apps.registry import Apps
from django.db import migrations, models
from django.utils import timezone


def retire_pending_news_suggestions(apps: Apps, _schema_editor: object) -> None:
    suggestions = apps.get_model("core", "PendingArticleSuggestion")
    suggestions.objects.filter(status="pending").update(
        status="dropped",
        decision_reason="Retired when news scans began settling candidates autonomously",
        decided_at=timezone.now(),
    )


class Migration(migrations.Migration):
    dependencies = [("core", "0027_retire_operational_questions")]
    operations = [
        migrations.AddField(
            model_name="pendingarticlesuggestion",
            name="decision_reason",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AlterField(
            model_name="pendingarticlesuggestion",
            name="status",
            field=models.CharField(
                choices=[
                    ("pending", "Pending"),
                    ("approved", "Approved"),
                    ("rejected", "Rejected"),
                    ("filed", "Filed"),
                    ("dropped", "Dropped"),
                ],
                default="pending",
                max_length=16,
            ),
        ),
        migrations.RunPython(retire_pending_news_suggestions, migrations.RunPython.noop),
    ]
