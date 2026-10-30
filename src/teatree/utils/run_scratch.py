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

_OWNER_ACCESS = 0o700
_INTERRUPTING_SIGNALS = (signal.SIGTERM, signal.SIGHUP)


def _interrupt(_signum: int, _frame: FrameType | None) -> None:
    raise KeyboardInterrupt


def _grant_owner_access(root: Path) -> None:
    pending = [root]
    while pending:
        directory = pending.pop()
        directory.chmod(_OWNER_ACCESS)
        pending.extend(child for child in directory.iterdir() if child.is_dir() and not child.is_symlink())


def remove_tree(root: Path) -> None:
    try:
        shutil.rmtree(root)
    except PermissionError:
        _grant_owner_access(root)
        shutil.rmtree(root)


@contextmanager
def run_scratch_dir(prefix: str, *, keep_on_success: bool = False) -> Generator[Path]:
    root = Path(tempfile.mkdtemp(prefix=prefix))
    handlers = []
    completed = False
    try:
        if threading.current_thread() is threading.main_thread():
            # A signal the launcher ignored (nohup) stays ignored.
            handlers.extend(
                (signum, signal.signal(signum, _interrupt))
                for signum in _INTERRUPTING_SIGNALS
                if signal.getsignal(signum) != signal.SIG_IGN
            )
        yield root
        completed = True
    finally:
        try:
            if not completed or not keep_on_success:
                try:
                    remove_tree(root)
                except OSError:
                    logger.warning("run scratch cleanup failed for %s", root, exc_info=True)
        finally:
            for signum, handler in handlers:
                signal.signal(signum, signal.SIG_DFL if handler is None else handler)
