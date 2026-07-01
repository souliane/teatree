"""``manage.py loop_drain_queue`` — one reactive DB-queue drain cycle (#786, #1052).

The dedicated driver for the django-tasks DB queue drain — a reactive ``/loop``
slot alongside Slack-answer and self-improve. Acquires the ``loop-drain-queue``
``LoopLease`` (so two sessions never drain the same rows and a slow drain never
blocks a fast tick), runs :func:`teatree.loop.queue_drain.expire_then_drain`
(retire stale READY jobs, then drain a bounded batch of the fresh remainder), and
prints a one-line summary (or the JSON report when ``--json`` is passed).

No loop-owner gate is needed here (unlike ``loop_slack_answer`` /
``loop_self_improve``): the drain is a mechanical DB-queue drain with no
user-facing hijack surface, and the ``loop-drain-queue`` lease mutex plus the
``teatree-worker`` flock check (:func:`teatree.loop.queue_drain.a_worker_is_running`)
already make concurrent drainers impossible.
"""

import datetime as dt
import json
import os
from typing import Annotated

import typer
from django_typer.management import TyperCommand


class Command(TyperCommand):
    help = "Run one reactive DB-queue drain cycle (expire stale READY jobs, then drain a bounded batch)."

    def handle(
        self,
        *,
        json_output: Annotated[bool, typer.Option("--json", help="Emit the cycle report as JSON.")] = False,
    ) -> None:
        from teatree.core.models import LoopLease  # noqa: PLC0415
        from teatree.loop.queue_drain import expire_then_drain  # noqa: PLC0415

        owner = f"pid-{os.getpid()}"
        if not LoopLease.objects.acquire("loop-drain-queue", owner=owner):
            now = dt.datetime.now(tz=dt.UTC)
            if json_output:
                self.stdout.write(
                    json.dumps(
                        {
                            "skipped": True,
                            "skipped_reason": "another drain cycle is already running",
                            "started_at": now.isoformat(),
                        },
                        indent=2,
                    )
                )
            else:
                self.stdout.write("SKIP  loop-drain-queue lease held — another cycle is running.")
            return
        try:
            result = expire_then_drain()
        finally:
            LoopLease.objects.release("loop-drain-queue", owner=owner)

        if json_output:
            self.stdout.write(json.dumps(result, indent=2, default=str))
            return
        retired = result["retired"]
        retired_total = sum(retired.values()) if isinstance(retired, dict) else 0
        self.stdout.write(f"OK    retired={retired_total} drained={result['drained']}")
