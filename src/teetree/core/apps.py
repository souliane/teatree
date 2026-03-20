import atexit
import contextlib
import logging
import os
import subprocess as _subprocess  # noqa: S404
import sys
import threading

from django.apps import AppConfig
from django.conf import settings

logger = logging.getLogger(__name__)

_SYNC_INTERVAL = 300  # 5 minutes
_worker_processes: list[_subprocess.Popen] = []


def _start_periodic_sync() -> None:
    """Enqueue sync_followup every 5 minutes in a daemon thread."""
    from teetree.core.tasks import sync_followup  # noqa: PLC0415

    def _loop() -> None:
        while True:
            threading.Event().wait(_SYNC_INTERVAL)
            try:
                sync_followup.enqueue()
                logger.info("Periodic followup sync enqueued")
            except Exception:
                logger.exception("Periodic followup sync failed to enqueue")

    thread = threading.Thread(target=_loop, daemon=True, name="teetree-periodic-sync")
    thread.start()


def _cleanup_workers() -> None:
    """Terminate all background worker processes."""
    for p in _worker_processes:
        with contextlib.suppress(OSError):
            p.terminate()
    for p in _worker_processes:
        try:
            p.wait(timeout=5)
        except _subprocess.TimeoutExpired:
            p.kill()
    _worker_processes.clear()


def _start_workers() -> None:
    """Spawn taskrunner subprocesses for background task execution."""
    count = getattr(settings, "TEATREE_WORKER_COUNT", 3)
    manage_py = str(settings.BASE_DIR / "manage.py")
    env = {**os.environ, "_TEETREE_WORKER": "1"}

    for _ in range(count):
        p = _subprocess.Popen(  # noqa: S603
            [sys.executable, manage_py, "db_worker", "--interval", "1", "--no-startup-delay", "--no-reload"],
            env=env,
            cwd=settings.BASE_DIR,
        )
        _worker_processes.append(p)
        logger.info("Spawned db_worker pid=%d", p.pid)

    atexit.register(_cleanup_workers)
    logger.info("Started %d background worker(s)", count)


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "teetree.core"
    verbose_name = "TeaTree Core"

    def ready(self) -> None:  # noqa: PLR6301
        if os.environ.get("RUN_MAIN") == "true" and not os.environ.get("_TEETREE_WORKER") and "runserver" in sys.argv:
            _start_periodic_sync()
            _start_workers()
