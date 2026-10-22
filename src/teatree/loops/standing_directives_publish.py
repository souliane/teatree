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
import logging
import time
from typing import TYPE_CHECKING

from django.tasks import TaskResultStatus, task
from django.utils import timezone
from django_tasks_db.models import DBTaskResult

from teatree import standing_directives_cache
from teatree.core.backend_factory import OwnerMessagingTransport
from teatree.core.models import PendingChatInjection
from teatree.core.slack_first.factory_slack_liveness import factory_slack_live
from teatree.loop.standing_directives import publish
from teatree.loops.timer_chains import LOOPS_QUEUE

if TYPE_CHECKING:
    from teatree.standing_directives_cache import OpenRelayPayload

logger = logging.getLogger(__name__)

#: How stale the published directives may get behind an admin edit or a mode change.
PUBLISH_POLL_SECONDS = 60


def _pending_publish() -> bool:
    return DBTaskResult.objects.filter(
        task_path=publish_standing_directives.module_path,
        status=TaskResultStatus.READY,
    ).exists()


def publish_factory_slack() -> None:
    """Publish whether factory Slack is live, per credentialed overlay, and each session's open relays.

    Rewritten every fire to stay fresh. The no-overlay verdict is ambiguous once several overlays are
    credentialed, so a session reads the one for its own overlay, as the relay command does.
    """
    verdict = factory_slack_live("")
    by_overlay = {name: factory_slack_live(name).live for name, _backend in OwnerMessagingTransport.credentialed()}
    open_relays: dict[str, list[OpenRelayPayload]] = {}
    for row in PendingChatInjection.objects.open_relays():
        open_relays.setdefault(row.relayed_by_session, []).append(row.open_relay_payload())
    standing_directives_cache.write_factory_slack(
        {
            "overlay": verdict.overlay,
            "live": verdict.live,
            "reason": verdict.reason,
            "checked_at": time.time(),
            "open_relays": open_relays,
            "by_overlay": by_overlay,
        }
    )


@task(queue_name=LOOPS_QUEUE)
def publish_standing_directives() -> dict[str, str]:
    """One fire: queue the successor, publish the directives if their resolution changed, refresh the Slack verdict."""
    if _pending_publish():
        return {"action": "deduped"}
    publish_standing_directives.using(run_after=timezone.now() + dt.timedelta(seconds=PUBLISH_POLL_SECONDS)).enqueue()
    action = "published" if publish() else "unchanged"
    try:
        publish_factory_slack()
    except Exception:
        logger.exception("could not publish the factory-Slack verdict; the hooks read it as absent and fail open")
    return {"action": action}


def ensure_standing_directives_publish_chain() -> None:
    """Seed the chain head if absent, due at once so a worker start publishes before its first poll."""
    if not _pending_publish():
        publish_standing_directives.using(run_after=timezone.now()).enqueue()


__all__ = ["PUBLISH_POLL_SECONDS", "ensure_standing_directives_publish_chain", "publish_standing_directives"]
