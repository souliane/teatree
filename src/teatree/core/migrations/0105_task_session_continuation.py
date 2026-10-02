from django.db import migrations, models


def mark_resumable_tasks(apps, schema_editor):
    """Type the IN-FLIGHT rows that were already resumable under the phase-equality rule.

    Defaulting every row to ``fresh`` would land mid-deploy on a queued needs-input
    continuation and drop the owner's answer, and on a limit-parked row and re-pay its
    whole accumulated context.
    """
    task_model = apps.get_model("core", "Task")
    attempt_model = apps.get_model("core", "TaskAttempt")
    task_model.objects.filter(status="pending", not_before__isnull=False).update(session_continuation="self")
    parked_task_ids = attempt_model.objects.filter(result__needs_user_input=True).values_list("task_id", flat=True)
    task_model.objects.filter(
        status__in=("pending", "claimed"),
        parent_task_id__in=parked_task_ids,
        execution_reason__startswith="The user answered your earlier question:",
    ).update(session_continuation="parent")


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0104_move_overlay_registry_pass_keys_onto_settings"),
    ]

    operations = [
        migrations.AddField(
            model_name="task",
            name="session_continuation",
            field=models.CharField(
                choices=[("fresh", "Fresh"), ("parent", "Resume parent"), ("self", "Resume self")],
                db_default="fresh",
                default="fresh",
                max_length=8,
            ),
        ),
        migrations.RunPython(mark_resumable_tasks, migrations.RunPython.noop),
    ]
