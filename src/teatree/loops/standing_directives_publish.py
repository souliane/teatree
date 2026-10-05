"""The self-rescheduling standing-directive publish chain — keeps the hooks' cache current (#4166).

The hooks deliver the standing directives without Django, from the file
:func:`teatree.loop.standing_directives.publish` writes. ``t3 loop directives disable|enable``
publishes at once, but an owner's edit in the admin and a mode change happen elsewhere, so a single
``publish_standing_directives`` job on the shared :data:`~teatree.loops.timer_chains.LOOPS_QUEUE`
re-resolves every :data:`PUBLISH_POLL_SECONDS` and rewrites the file only when the resolution changed.
Zero-token and deterministic, like the ``statusline_refresh`` chain it is modelled on. Seeded by
:func:`teatree.loops.timer_reconciler.ensure_maintenance_chains` at worker startup and
self-perpetuating after that; the successor is queued before the publish, so a failed publish never
ends the chain.
"""

import datetime as dt

from django.tasks import TaskResultStatus, task
from django.utils import timezone
from django_tasks_db.models import DBTaskResult

from teatree.loop.standing_directives import publish
from teatree.loops.timer_chains import LOOPS_QUEUE

#: How stale the published directives may get behind an admin edit or a mode change.
PUBLISH_POLL_SECONDS = 60


def _pending_publish() -> bool:
    return DBTaskResult.objects.filter(
        task_path=publish_standing_directives.module_path,
        status=TaskResultStatus.READY,
    ).exists()


@task(queue_name=LOOPS_QUEUE)
def publish_standing_directives() -> dict[str, str]:
    """One fire: queue the successor, then publish the directives if their resolution changed."""
    if _pending_publish():
        return {"action": "deduped"}
    publish_standing_directives.using(run_after=timezone.now() + dt.timedelta(seconds=PUBLISH_POLL_SECONDS)).enqueue()
    return {"action": "published" if publish() else "unchanged"}


def ensure_standing_directives_publish_chain() -> None:
    """Seed the chain head if absent, due at once so a worker start publishes before its first poll."""
    if not _pending_publish():
        publish_standing_directives.using(run_after=timezone.now()).enqueue()


__all__ = ["PUBLISH_POLL_SECONDS", "ensure_standing_directives_publish_chain", "publish_standing_directives"]
