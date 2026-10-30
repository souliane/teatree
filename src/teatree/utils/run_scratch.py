"""Own the lifetime of a temporary directory used by a running command."""

import logging
import shutil
import signal
import tempfile
import threading
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from types import FrameType

logger = logging.getLogger(__name__)


def _interrupt(_signum: int, _frame: FrameType | None) -> None:
    raise KeyboardInterrupt


@contextmanager
def run_scratch_dir(prefix: str, *, keep_on_success: bool = False) -> Generator[Path]:
    root = Path(tempfile.mkdtemp(prefix=prefix))
    handlers = []
    completed = False
    try:
        if threading.current_thread() is threading.main_thread():
            handlers.extend((signum, signal.signal(signum, _interrupt)) for signum in (signal.SIGTERM, signal.SIGHUP))
        yield root
        completed = True
    finally:
        try:
            if not completed or not keep_on_success:
                try:
                    shutil.rmtree(root)
                except OSError:
                    logger.warning("run scratch cleanup failed for %s", root, exc_info=True)
        finally:
            for signum, handler in handlers:
                signal.signal(signum, signal.SIG_DFL if handler is None else handler)
