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

:func:`wait_out_locked_writes` is the connection-level twin for a finished run that must
not be lost to a lock: a scope (or decorator) re-issuing one statement at a time, so it
covers every ORM write inside without re-running any block. Use it where the code is not
idempotent as a whole; use :func:`retry_on_locked` where the callable owns its transaction.
"""

import logging
import sqlite3
import time
from collections.abc import Callable, Generator
from contextlib import AbstractContextManager, contextmanager
from typing import TYPE_CHECKING, cast

from django.db import DatabaseError, OperationalError, ProgrammingError, connections

if TYPE_CHECKING:
    from django.db.backends.base.base import BaseDatabaseWrapper

logger = logging.getLogger(__name__)

DEFAULT_ATTEMPTS = 5
DEFAULT_BASE_DELAY = 0.05

_FIRST_DELAY = 0.5
_MAX_DELAY = 8.0
_WAIT_BUDGET = 60.0

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


class _WaitOutLockedStatements:
    """Django ``execute_wrapper`` re-issuing one statement that lost the SQLite write lock."""

    def __init__(self, connection: "BaseDatabaseWrapper") -> None:
        self.connection = connection

    def __call__(self, execute: Callable[..., object], sql: str, params: object, *forwarded: object) -> object:
        many, _context = forwarded
        give_up_at = time.monotonic() + _WAIT_BUDGET
        delay = _FIRST_DELAY
        while True:
            try:
                return execute(sql, params, *forwarded)
            except OperationalError as exc:
                raw_connection = self.connection.connection
                transaction_open = self.connection.in_atomic_block or (
                    isinstance(raw_connection, sqlite3.Connection) and raw_connection.in_transaction
                )
                if many or transaction_open or not is_locked_error(exc) or time.monotonic() >= give_up_at:
                    raise
                logger.info("db_retry: statement hit 'database is locked' — re-issuing in %.1fs", delay)
                time.sleep(delay)
                delay = min(delay * 2, _MAX_DELAY)


@contextmanager
def wait_out_locked_writes(using: str = "default") -> Generator[None]:
    """Re-issue any statement of *using* that fails on ``database is locked``, within a bounded wait.

    A statement is re-issued only outside a transaction (``BEGIN IMMEDIATE`` runs before the
    block opens, so an ``atomic()`` entry is covered) and never as ``executemany``; any other
    error surfaces at once. Reentrant: an inner scope adds nothing, so the budget never doubles.
    """
    connection = connections[using]
    if any(isinstance(wrapper, _WaitOutLockedStatements) for wrapper in connection.execute_wrappers):
        yield
    else:
        # Django's ``execute_wrapper`` is an unannotated ``@contextmanager``.
        scope = cast("AbstractContextManager[None]", connection.execute_wrapper(_WaitOutLockedStatements(connection)))
        with scope:
            yield
