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
from typing import Annotated

import typer
from django_typer.management import TyperCommand, command

logger = logging.getLogger(__name__)


class Command(TyperCommand):
    @command()
    def status(self) -> None:
        """Print the queue breakdown by status, and READY jobs by task name."""
        from django_tasks.base import TaskResultStatus  # noqa: PLC0415
        from django_tasks_db.models import DBTaskResult  # noqa: PLC0415

        total = DBTaskResult.objects.count()
        self.stdout.write(f"Total queued rows: {total}")
        for value in TaskResultStatus.values:
            self.stdout.write(f"  {value}: {DBTaskResult.objects.filter(status=value).count()}")
        ready = DBTaskResult.objects.filter(status=TaskResultStatus.READY)
        if ready.exists():
            self.stdout.write("READY by task:")
            for name, count in Counter(job.task_name for job in ready.iterator()).most_common():
                self.stdout.write(f"  {count}  {name}")

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
        import datetime as dt  # noqa: PLC0415

        from django.utils import timezone  # noqa: PLC0415
        from django_tasks.base import TaskResultStatus  # noqa: PLC0415
        from django_tasks_db.models import DBTaskResult  # noqa: PLC0415

        from teatree.loop.queue_drain import expire_stale_ready_jobs, stale_threshold_hours  # noqa: PLC0415

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
