"""Bounded retry-on-``database is locked`` for the canonical-DB writes (souliane/teatree#1520).

The canonical control DB is file-backed SQLite. ``settings`` already sets a
30s ``busy_timeout`` and ``BEGIN IMMEDIATE`` write-serialization, yet under the
autonomous factory's intended steady state (the durable loop's merge ceremony
running concurrently with N fix-agents writing the same DB) a momentary lock
can still surface as ``OperationalError: database is locked`` and abort the
merge keystone mid-flight.

:func:`is_locked_error` and :func:`is_missing_table_error` are the shared
classification of a ``DatabaseError``: a transient lock, a pre-migration missing
relation, or a real failure that must surface.

:func:`retry_on_locked` wraps a DB-write callable in a small bounded retry with
exponential backoff. Only a transient ``database is locked`` is retried — a
non-transient ``OperationalError`` (``no such table``, ``malformed``, …) and a
genuinely stuck lock (after the cap) still surface, so the helper never swallows
a real failure. The wrapped callable must be idempotent on retry: the merge
post hook re-reads the CLEAR ``select_for_update``-locked and re-asserts the
single-use guard, so re-running it consumes the CLEAR exactly once.
"""

import logging
import time
from collections.abc import Callable

from django.db import DatabaseError, OperationalError, ProgrammingError

logger = logging.getLogger(__name__)

DEFAULT_ATTEMPTS = 5
DEFAULT_BASE_DELAY = 0.05

_LOCKED_MARKER = "database is locked"
_MISSING_TABLE_MARKERS = ("no such table", "does not exist", "doesn't exist", "undefined table")


def is_locked_error(exc: OperationalError) -> bool:
    """True iff *exc* is the transient SQLite ``database is locked`` (SQLITE_BUSY)."""
    return _LOCKED_MARKER in str(exc).lower()


def is_missing_table_error(exc: DatabaseError) -> bool:
    """True iff *exc* is a pre-migration missing relation, not a live-DB failure.

    A ``ProgrammingError`` is the schema class by construction; an
    ``OperationalError`` is the same class only when it names the missing
    relation (SQLite reports it there, and so a busy or corrupt DB stays
    distinguishable from an unmigrated one).
    """
    if isinstance(exc, ProgrammingError):
        return True
    text = str(exc).lower()
    return any(marker in text for marker in _MISSING_TABLE_MARKERS)


def retry_on_locked[T](
    operation: Callable[[], T],
    *,
    attempts: int = DEFAULT_ATTEMPTS,
    base_delay: float = DEFAULT_BASE_DELAY,
) -> T:
    """Run *operation*, retrying only on a transient ``database is locked``.

    Exponential backoff (``base_delay``, doubling each retry) up to *attempts*
    total tries. A non-transient ``OperationalError`` or any other exception
    propagates immediately; a still-locked DB after the final attempt re-raises
    the last lock error so a genuinely stuck lock surfaces a clear failure.
    """
    for attempt in range(attempts):
        try:
            return operation()
        except OperationalError as exc:
            if not is_locked_error(exc) or attempt == attempts - 1:
                raise
            delay = base_delay * (2**attempt)
            logger.info(
                "db_retry: transient 'database is locked' on attempt %d/%d — backing off %.3fs",
                attempt + 1,
                attempts,
                delay,
            )
            time.sleep(delay)
    raise OperationalError(_LOCKED_MARKER)  # pragma: no cover — attempts>=1 guarantees the loop returns or raises
