"""``manage.py worker`` — run the singleton loop-timer worker (#1796).

Acquires the ``worker`` flock singleton (:func:`teatree.utils.singleton.singleton`)
so at most one worker drains the shared queue per box, installs SIGTERM/SIGINT
handlers that ask the supervisor to shut down, then runs the executor pool
(:class:`teatree.loops.worker.LoopWorker`). A second invocation while a worker is
alive refuses immediately with a non-zero exit (the ``flock`` is the lock; the pid
in the file is diagnostic only).
"""

import signal
from types import FrameType

from django_typer.management import TyperCommand


class Command(TyperCommand):
    help = "Run the singleton loop-timer worker (#1796) — K pinned executors, no OS scheduler."

    def handle(self) -> None:
        from teatree.loops.worker import WORKER_SINGLETON, LoopWorker  # noqa: PLC0415
        from teatree.utils.singleton import AlreadyRunningError, singleton  # noqa: PLC0415

        try:
            with singleton(WORKER_SINGLETON):
                worker = LoopWorker()

                def _shutdown(_signum: int, _frame: FrameType | None) -> None:
                    worker.request_stop()

                signal.signal(signal.SIGTERM, _shutdown)
                signal.signal(signal.SIGINT, _shutdown)
                worker.run()
        except AlreadyRunningError as exc:
            self.stderr.write(str(exc))
            raise SystemExit(1) from exc
