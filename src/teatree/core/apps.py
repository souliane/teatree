import atexit
import contextlib
import logging
import os
import sys
import threading

from django.apps import AppConfig
from django.conf import settings

from teatree.utils.run import Popen, TimeoutExpired, spawn

logger = logging.getLogger(__name__)

_DRAIN_INTERVAL = 10  # seconds — check for pending headless tasks
_SYNC_INTERVAL = 300  # 5 minutes — full followup sync
_worker_processes: list[Popen[str]] = []


def _start_periodic_sync() -> None:
    """Enqueue sync_followup and drain_headless_queue periodically."""
    from teatree.core.tasks import drain_headless_queue, sync_followup  # noqa: PLC0415

    def _loop() -> None:
        tick = 0
        while True:
            threading.Event().wait(_DRAIN_INTERVAL)
            tick += 1
            try:
                drain_headless_queue.enqueue()
            except Exception:
                logger.exception("Periodic headless drain failed to enqueue")
            if tick % (_SYNC_INTERVAL // _DRAIN_INTERVAL) == 0:
                try:
                    sync_followup.enqueue()
                    logger.info("Periodic followup sync enqueued")
                except Exception:
                    logger.exception("Periodic followup sync failed to enqueue")

    thread = threading.Thread(target=_loop, daemon=True, name="teatree-periodic-sync")
    thread.start()


def _cleanup_workers() -> None:
    """Terminate all background worker processes."""
    for p in _worker_processes:
        with contextlib.suppress(OSError):
            p.terminate()
    for p in _worker_processes:
        try:
            p.wait(timeout=5)
        except TimeoutExpired:
            p.kill()
    _worker_processes.clear()


def _start_workers() -> None:
    """Spawn taskrunner subprocesses for background task execution."""
    count = getattr(settings, "TEATREE_WORKER_COUNT", 3)
    env = {**os.environ, "_TEETREE_WORKER": "1"}

    for _ in range(count):
        p = spawn(
            [sys.executable, "-m", "teatree", "db_worker", "--interval", "1", "--no-startup-delay", "--no-reload"],
            env=env,
        )
        _worker_processes.append(p)
        logger.info("Spawned db_worker pid=%d", p.pid)

    atexit.register(_cleanup_workers)
    logger.info("Started %d background worker(s)", count)


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "teatree.core"
    verbose_name = "TeaTree Core"

    def ready(self) -> None:  # noqa: PLR6301
        from teatree.core.signals import register_signals  # noqa: PLC0415

        register_signals()

        is_server = "runserver" in sys.argv or "uvicorn" in sys.argv[0]
        if os.environ.get("RUN_MAIN") == "true" and not os.environ.get("_TEETREE_WORKER") and is_server:
            _start_periodic_sync()
            _start_workers()
