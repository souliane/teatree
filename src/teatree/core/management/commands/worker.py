"""``manage.py worker`` — run the singleton loop-timer worker (#1796).

Acquires the ``worker`` flock singleton (:func:`teatree.utils.singleton.singleton`)
so at most one worker drains the shared queue per box, installs SIGTERM/SIGINT
handlers that ask the supervisor to shut down, then runs the executor pool
(:class:`teatree.loops.worker.LoopWorker`). A second invocation while a worker is
alive refuses immediately with a non-zero exit (the ``flock`` is the lock; the pid
in the file is diagnostic only).

Every refusal is recorded against its REASON (#3976). The supervisor restarts a
refused worker indefinitely, so a race this deployment can never win — a singleton
held from outside the container, over a lock file the two share through a bind mount —
otherwise looks exactly like an ordinary restart cycle while the deployed worker never
runs at all. :data:`~teatree.utils.singleton_refusals.ESCALATION_THRESHOLD` identical
refusals in a row are reported as the failure they are, and ``t3 doctor check`` fails
on the standing streak until an acquire succeeds.
"""

import signal
from types import FrameType

from django_typer.management import TyperCommand

from teatree.utils.singleton import AlreadyRunningError

#: Exit code for a worker refused the same way N times running — distinct from the 1 an
#: ordinary (possibly transient) refusal exits with, so a supervisor or a `docker inspect`
#: can tell a starved worker from a lost deploy hand-over.
STARVED_EXIT = 5


class Command(TyperCommand):
    help = "Run the singleton loop-timer worker (#1796) — K pinned executors, no OS scheduler."

    def handle(self) -> None:
        from teatree.loops.worker import LoopWorker  # noqa: PLC0415 — deferred: pulls the timer machinery
        from teatree.utils.singleton import WORKER_SINGLETON, singleton  # noqa: PLC0415 — deferred with LoopWorker
        from teatree.utils.singleton_refusals import clear_refusals  # noqa: PLC0415 — deferred with LoopWorker

        try:
            with singleton(WORKER_SINGLETON):
                clear_refusals(WORKER_SINGLETON)
                worker = LoopWorker()

                def _shutdown(_signum: int, _frame: FrameType | None) -> None:
                    worker.request_stop()

                signal.signal(signal.SIGTERM, _shutdown)
                signal.signal(signal.SIGINT, _shutdown)
                worker.run()
        except AlreadyRunningError as exc:
            raise SystemExit(self._report_refusal(exc)) from exc

    def _report_refusal(self, exc: AlreadyRunningError) -> int:
        """Record the refusal against its reason and return the exit code it earns."""
        from teatree.utils.singleton_refusals import record_refusal  # noqa: PLC0415 — deferred: refusal path only

        streak = record_refusal(exc.name, fingerprint=exc.reason_fingerprint)
        self.stderr.write(str(exc))
        if not streak.escalated:
            return 1
        self.stderr.write(
            f"FATAL this worker has now been refused {streak.count} times running for the SAME reason — "
            "restarting cannot resolve it, and every refused boot re-runs the full provisioning pass first. "
            "Stop the holder named above (`t3 worker stop`, or end the process outside this runtime) so the "
            "deployed worker can start. `t3 doctor check` fails on this until an acquire succeeds."
        )
        return STARVED_EXIT
