"""A hand-wound clock for the board janitor's deadline-bounded reads."""

import contextlib
import threading
from collections.abc import Generator
from unittest.mock import patch


class FakeClock:
    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now
        self._lock = threading.Lock()

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        with self._lock:
            self.now += seconds


@contextlib.contextmanager
def read_clock(clock: FakeClock) -> Generator[None]:
    with patch("teatree.loop.scanners.board_reconcile_reads.monotonic", clock):
        yield
