import django.db.models.deletion
from django.db import migrations, models

_LOOP_SESSION_ROWS = (
    models.Q(dedupe_marker__startswith="news-batch-")
    | models.Q(dedupe_marker__startswith="triage-batch-")
    | models.Q(question__startswith="Approve this drafted reply")
)


def _leave_only_asking_sessions(apps, schema_editor):
    """A loop session that happens to be all digits is blanked before the digit loop can read it as a Session."""
    question = apps.get_model("core", "DeferredQuestion")
    session = apps.get_model("core", "Session")
    alias = schema_editor.connection.alias
    question.objects.using(alias).filter(_LOOP_SESSION_ROWS).exclude(session_id="").update(session_id="")
    questions = question.objects.using(alias)
    rows = list(questions.filter(session_id__regex=r"^[0-9]+$"))
    numbers = {int(row.session_id) for row in rows}
    known = set(session.objects.using(alias).filter(pk__in=numbers).values_list("pk", flat=True))
    for row in rows:
        task_session = int(row.session_id)
        questions.filter(pk=row.pk).update(
            task_session_id=task_session if task_session in known else None, session_id=""
        )


def _restore_session_ids(apps, schema_editor):
    question = apps.get_model("core", "DeferredQuestion")
    alias = schema_editor.connection.alias
    questions = question.objects.using(alias)
    for row in questions.filter(task_session__isnull=False, session_id=""):
        questions.filter(pk=row.pk).update(session_id=str(row.task_session_id))


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0120_merge_banned_term_registry"),
    ]

    operations = [
        migrations.AddField(
            model_name="deferredquestion",
            name="task_session",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to="core.session",
            ),
        ),
        migrations.RunPython(_leave_only_asking_sessions, _restore_session_ids),
    ]
