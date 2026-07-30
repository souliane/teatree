from django.db import migrations
from django.db.models import Max
from django.db.models.functions import Coalesce, Greatest

# ``Task.Status.active()`` as literals — a historical model carries no methods.
_ACTIVE_TASK_STATUSES = ("pending", "claimed")


def _close_orphaned_sessions(apps, schema_editor):
    """Close every open Session no longer owning active work, at its last activity.

    ``Session.ended_at`` had no production writer, so every session ever minted
    is still open and reads as live — which pins its ticket busy and stalls the
    worktree reaper, the idle-stack reaper and ``workspace relocate``.

    Each row closes at its LAST RECORDED ACTIVITY (its own ``started_at``, the
    last heartbeat of a task it owns, or that task's most recent attempt start),
    never at ``now()`` — stamping ``now()`` would leave every row inside the
    ``session_stale_after_hours`` window and re-pin exactly what this frees.

    A session still owning a PENDING/CLAIMED task is left OPEN: it may be
    genuinely in flight, and the reapers must fail closed. Idempotent — a re-run
    matches only rows that are still open.
    """
    Session = apps.get_model("core", "Session")

    busy_pks = set(Session.objects.filter(tasks__status__in=_ACTIVE_TASK_STATUSES).values_list("pk", flat=True))
    rows = list(
        Session.objects.filter(ended_at__isnull=True)
        .exclude(pk__in=busy_pks)
        .annotate(
            last_activity=Greatest(
                "started_at",
                Coalesce(Max("tasks__heartbeat_at"), "started_at"),
                Coalesce(Max("tasks__attempts__started_at"), "started_at"),
            ),
        ),
    )
    for session in rows:
        session.ended_at = session.last_activity
    Session.objects.bulk_update(rows, ["ended_at"], batch_size=500)


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0031_taskattempt_reasoning_effort_and_more"),
    ]

    operations = [
        migrations.RunPython(_close_orphaned_sessions, migrations.RunPython.noop),
    ]
