from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations.state import StateApps
from django.db.models import F


def move_legacy_delivery(apps: StateApps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    question = apps.get_model("core", "DeferredQuestion")
    question.objects.using(schema_editor.connection.alias).filter(applied_at__isnull=False).update(
        delivered_at=F("applied_at"), applied_at=None
    )


def restore_legacy_delivery(apps: StateApps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    question = apps.get_model("core", "DeferredQuestion")
    question.objects.using(schema_editor.connection.alias).filter(delivered_at__isnull=False).update(
        applied_at=F("delivered_at")
    )


class Migration(migrations.Migration):
    dependencies = [("core", "0019_merge_sealedsecret_forge_ingest")]

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
                    ("delegate", "Delegated under Rule -1"),
                ],
                default="",
                max_length=8,
            ),
        ),
        migrations.AddField(
            model_name="deferredquestion",
            name="delivered_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(move_legacy_delivery, restore_legacy_delivery),
    ]
