"""Recover the failure reasons that 0049 left blank.

``Task.failure_reason`` / ``failure_kind`` and ``TaskAttempt.failure_kind`` were added as
bare ``AddField`` operations with no data migration, so every row that had already failed
took the column default. The text was never lost — ``TaskAttempt.error`` has carried it
since day one — but nothing joined the two, so a listing showed a cause-less failure and
an operator could not tell a genuine defect from a lost lease or an exhausted credential.

That is the exact harm ``FailureKind.UNRECORDED`` names: a blank reason is a defect in
the path that produced it, not a property of the failure. Here the producing path was a
schema change, and this is its missing half.

Reads only; a row whose attempts recorded nothing keeps its blank reason and is left for
``UNRECORDED`` to name, because inventing a cause is worse than admitting none.
"""

from django.db import migrations

from teatree.core.modelkit.task_failure_taxonomy import classify_failure

_BATCH = 500


def _latest_error(attempts) -> str:
    """The newest non-blank error among *attempts* — the reason the task actually failed."""
    for attempt in attempts:
        if attempt.error.strip():
            return attempt.error
    return ""


def backfill(apps, schema_editor) -> None:
    task_model = apps.get_model("core", "Task")
    attempt_model = apps.get_model("core", "TaskAttempt")

    stale_tasks = task_model.objects.filter(status="failed", failure_reason="").prefetch_related("attempts")
    repaired = []
    for task in stale_tasks.iterator(chunk_size=_BATCH):
        error = _latest_error(sorted(task.attempts.all(), key=lambda a: a.id, reverse=True))
        if not error:
            continue
        task.failure_reason = error
        task.failure_kind = classify_failure(error)
        repaired.append(task)
        if len(repaired) >= _BATCH:
            task_model.objects.bulk_update(repaired, ["failure_reason", "failure_kind"])
            repaired.clear()
    if repaired:
        task_model.objects.bulk_update(repaired, ["failure_reason", "failure_kind"])

    # The attempt rows carry the error but were never re-saved, so their kind stayed blank
    # while the text sat right beside it.
    stale_attempts = attempt_model.objects.exclude(error="").filter(failure_kind="")
    classified = []
    for attempt in stale_attempts.iterator(chunk_size=_BATCH):
        attempt.failure_kind = classify_failure(attempt.error)
        classified.append(attempt)
        if len(classified) >= _BATCH:
            attempt_model.objects.bulk_update(classified, ["failure_kind"])
            classified.clear()
    if classified:
        attempt_model.objects.bulk_update(classified, ["failure_kind"])


class Migration(migrations.Migration):
    dependencies = [("core", "0053_resourcepressuremarker_adaptive_intake_concurrency_and_more")]

    operations = [migrations.RunPython(backfill, migrations.RunPython.noop, elidable=True)]
