from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar

_SCAN_DEADLINE: ContextVar[float | None] = ContextVar("scan_deadline", default=None)


@contextmanager
def scanner_deadline(deadline: float | None) -> Generator[None]:
    token = _SCAN_DEADLINE.set(deadline)
    try:
        yield
    finally:
        _SCAN_DEADLINE.reset(token)


def current_scan_deadline() -> float | None:
    return _SCAN_DEADLINE.get()
