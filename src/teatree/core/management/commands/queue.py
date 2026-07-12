"""``manage.py queue`` — inspect and expire the django-tasks DB queue.

``expire-stale`` retires READY jobs older than a threshold (default
``T3_QUEUE_STALE_HOURS``, 24h) to the terminal ``FAILED`` state so a
freshly-supervised drainer never blind-fires ancient heavy jobs. ``status``
reports the queue breakdown without mutating anything.

The drain itself rides the loop tick (see :mod:`teatree.loop.queue_drain`);
this command is the operator surface for the one-off backlog retirement and
for read-only inspection.
"""

import logging
from collections import Counter
from typing import IO, Annotated, TypedDict, cast

import typer
from django_typer.management import TyperCommand, command

from teatree.core.machine_output import emit
from teatree.core.table_output import print_table

logger = logging.getLogger(__name__)


class QueueStatus(TypedDict):
    total: int
    by_status: dict[str, int]
    ready_by_task: dict[str, int]


class Command(TyperCommand):
    @command()
    def status(
        self,
        *,
        json_output: Annotated[
            bool,
            typer.Option("--json", help="Emit the queue breakdown as JSON on stdout instead of the human view."),
        ] = False,
    ) -> QueueStatus:
        """Print the queue breakdown by status, and READY jobs by task name."""
        from django_tasks.base import TaskResultStatus  # noqa: PLC0415 — deferred: heavy/optional dep at call site
        from django_tasks_db.models import DBTaskResult  # noqa: PLC0415 — deferred: heavy/optional dep at call site

        total = DBTaskResult.objects.count()
        by_status = {value: DBTaskResult.objects.filter(status=value).count() for value in TaskResultStatus.values}
        ready = DBTaskResult.objects.filter(status=TaskResultStatus.READY)
        ready_by_task = dict(Counter(job.task_name for job in ready.iterator()).most_common())
        payload: QueueStatus = {"total": total, "by_status": by_status, "ready_by_task": ready_by_task}

        self.print_result = False
        emit(
            payload,
            json_output=json_output,
            out=cast("IO[str]", self.stdout),
            err=cast("IO[str]", self.stderr),
            human=lambda stream: _render_status(payload, stream),
        )
        return payload

    @command(name="expire-stale")
    def expire_stale(
        self,
        *,
        hours: Annotated[
            int,
            typer.Option(
                help="Expire READY jobs enqueued more than this many hours ago (default: T3_QUEUE_STALE_HOURS)."
            ),
        ] = 0,
        dry_run: Annotated[
            bool,
            typer.Option(help="Report what would be expired without mutating any rows."),
        ] = False,
    ) -> None:
        """Retire stale READY jobs to FAILED so a drainer never runs them.

        Conservative: only READY jobs older than the threshold are touched.
        FAILED is reversible — the row and its args are preserved — so an
        operator can re-enqueue a wrongly-retired job.
        """
        import datetime as dt  # noqa: PLC0415 — deferred: loaded only when this command runs

        from django.utils import timezone  # noqa: PLC0415 — deferred: Django import at call time
        from django_tasks.base import TaskResultStatus  # noqa: PLC0415 — deferred: heavy/optional dep at call site
        from django_tasks_db.models import DBTaskResult  # noqa: PLC0415 — deferred: heavy/optional dep at call site

        from teatree.loop.queue_drain import (  # noqa: PLC0415 — deferred: keeps command import light
            expire_stale_ready_jobs,
            stale_threshold_hours,
        )

        threshold = hours if hours > 0 else stale_threshold_hours()
        if dry_run:
            cutoff = timezone.now() - dt.timedelta(hours=threshold)
            stale = DBTaskResult.objects.filter(status=TaskResultStatus.READY, enqueued_at__lt=cutoff)
            counts = Counter(job.task_name for job in stale.iterator())
            self.stdout.write(f"DRY RUN — would expire {sum(counts.values())} READY job(s) older than {threshold}h:")
            for name, count in counts.most_common():
                self.stdout.write(f"  {count}  {name}")
            return

        retired = expire_stale_ready_jobs(threshold_hours=threshold)
        self.stdout.write(f"Expired {sum(retired.values())} READY job(s) older than {threshold}h:")
        for name, count in sorted(retired.items(), key=lambda kv: -kv[1]):
            self.stdout.write(f"  {count}  {name}")


def _render_status(payload: QueueStatus, stream: IO[str]) -> None:
    print_table(
        ["Status", "Count"],
        [[value, count] for value, count in payload["by_status"].items()],
        title=f"Queue — {payload['total']} rows",
        stream=stream,
        justify=["left", "right"],
    )
    if payload["ready_by_task"]:
        print_table(
            ["Task", "Ready"],
            [[name, count] for name, count in payload["ready_by_task"].items()],
            title="READY by task",
            stream=stream,
            justify=["left", "right"],
        )
