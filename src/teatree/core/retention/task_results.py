"""``DBTaskResult`` retention — the seam onto the library's own prune (#3871).

``django_tasks_db`` is a dependency, not teatree code, and it already ships retention:
``manage.py prune_db_task_results`` (``--min-age-days`` / ``--failed-min-age-days`` /
``--queue-name`` / ``--dry-run``). Configuring the dependency beats reaching into its
table, so the DELETE here is that command — teatree owns no second definition of which
result rows are disposable, and no second delete over someone else's schema.

Two adjustments the command's defaults need for this queue topology. Its
``--queue-name`` defaults to ``default`` alone, and every self-rescheduling chain rides
``loops`` (:data:`teatree.loops.timer_chains.LOOPS_QUEUE`), so the defaults would leave
the chain residue untouched — this passes ``*``. And its 14-day default window is far
too long for a table measured at ~400k finished rows per day: the window is a teatree
setting instead (:attr:`UserSettings.task_result_retention_days`).

:func:`prunable_task_results` is the read-only preview. It is expressed through the
library's OWN :meth:`DBTaskResultQuerySet.finished` manager method rather than a
hand-written status list, and
``tests/teatree_core/retention/test_task_results.py::test_deletes_exactly_what_the_dry_run_counted``
pins the preview to the delete — so a library predicate change surfaces as a red test
rather than a report that quietly disagrees with what was removed.
"""

import datetime as dt
from typing import TYPE_CHECKING

from django.core.management import call_command
from django_tasks import DEFAULT_TASK_BACKEND_ALIAS

if TYPE_CHECKING:
    from django_tasks_db.models import DBTaskResultQuerySet

#: Every configured queue. The command's own default is the single ``default`` queue,
#: which would silently exclude the ``loops`` chain rows that dominate the table.
_ALL_QUEUES = "*"


def task_results_are_stored_in_the_db() -> bool:
    """Whether the default task backend is the one that writes ``DBTaskResult`` rows.

    The library's prune command refuses any other backend, and an eager/immediate
    backend has no result table to prune. Checked so the lane can report itself
    inapplicable rather than taking the whole retention pass down with a
    ``CommandError`` — the other lanes' work is unrelated to this one's dependency.
    """
    from django_tasks import task_backends  # noqa: PLC0415 — deferred: heavy/optional dep at call site
    from django_tasks_db.backend import DatabaseBackend  # noqa: PLC0415 — deferred: heavy/optional dep at call site

    return isinstance(task_backends[DEFAULT_TASK_BACKEND_ALIAS], DatabaseBackend)


def prunable_task_results(cutoff: dt.datetime) -> "DBTaskResultQuerySet":
    """The finished result rows older than *cutoff* — read-only, deletes nothing.

    A READY or RUNNING row is never in the set: ``finished()`` is the library's own
    successful-or-failed predicate, and it is the same one the delete resolves through.
    """
    from django_tasks_db.models import DBTaskResult  # noqa: PLC0415 — deferred: heavy/optional dep at call site

    return DBTaskResult.objects.finished().filter(
        backend_name=DEFAULT_TASK_BACKEND_ALIAS,
        finished_at__lte=cutoff,
    )


def prune_finished_task_results(*, days: int) -> int:
    """Delete finished result rows older than *days*; return how many went.

    The count is measured across the library command rather than predicted, so it
    reports what actually happened even if the library's predicate differs from
    :func:`prunable_task_results`.
    """
    from django_tasks_db.models import DBTaskResult  # noqa: PLC0415 — deferred: heavy/optional dep at call site

    before = DBTaskResult.objects.count()
    call_command("prune_db_task_results", min_age_days=days, queue_name=_ALL_QUEUES, verbosity=0)
    return before - DBTaskResult.objects.count()


__all__ = ["prunable_task_results", "prune_finished_task_results", "task_results_are_stored_in_the_db"]
