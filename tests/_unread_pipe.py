"""A REAL pipe whose reader is gone, as ``sys.stdout``: the write a hook makes once the harness stopped reading."""

import contextlib
import os
from collections.abc import Iterator
from unittest.mock import patch


@contextlib.contextmanager
def unread_stdout() -> Iterator[None]:
    """Patch ``sys.stdout`` onto an OS pipe whose read end is closed, so its flush fails with EPIPE."""
    unread, written = os.pipe()
    os.close(unread)
    stream = os.fdopen(written, "w", encoding="utf-8")
    try:
        with patch("sys.stdout", stream):
            yield
    finally:
        with contextlib.suppress(OSError):
            stream.close()
