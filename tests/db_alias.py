"""Shared helpers for tests that drive a private, file-backed SQLite alias.

Migrates to HEAD without touching the shared ``default`` test database
(#2915). ``tests/`` is a package and sits on ``pythonpath`` (see
``pyproject.toml``), so both ``tests/teatree_core/conftest.py`` (not itself
a package) and top-level test modules can import from here without
duplicating the router or the alias register/teardown boilerplate.

:func:`run_racing_threads` serves the alias tests that race the real locking
primitive from real threads — see its docstring.
"""

import logging
import threading
from collections.abc import Callable
from pathlib import Path

from django.db import connections


class RouteAllToAlias:
    """Force every unscoped ORM query onto ``alias`` for one migrate call (#2915).

    The ``core`` ``0001_initial`` loop/prompt seed runs a ``RunPython`` that reads
    historical models via ``apps.get_model(...).objects`` with no
    ``.using(...)`` — Django resolves that to ``DEFAULT_DB_ALIAS`` regardless
    of which connection the surrounding ``migrate --database`` targets.
    Installing this as the sole ``DATABASE_ROUTERS`` entry for the migrate
    call reroutes those unscoped reads/writes onto the private alias instead
    of leaking onto the shared ``default`` connection.
    """

    def __init__(self, alias: str) -> None:
        self.alias = alias

    def db_for_read(self, model: type, **hints: object) -> str:
        return self.alias

    def db_for_write(self, model: type, **hints: object) -> str:
        return self.alias


def register_sqlite_alias(alias: str, db_file: Path) -> None:
    """Register a private, file-backed SQLite connection under ``alias``."""
    connections.databases[alias] = {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": str(db_file),
        "OPTIONS": {},
        "ATOMIC_REQUESTS": False,
        "AUTOCOMMIT": True,
        "CONN_MAX_AGE": 0,
        "CONN_HEALTH_CHECKS": False,
        "TIME_ZONE": None,
        "TEST": {},
    }


def teardown_sqlite_alias(alias: str) -> None:
    """Close and unregister a private alias registered via :func:`register_sqlite_alias`."""
    for conn in connections.all():
        if conn.alias == alias:
            conn.close()
    connections.databases.pop(alias, None)


def run_racing_threads[T](work: Callable[[int], T], count: int, *, timeout: float = 15.0) -> list[T]:
    """Run ``work(idx)`` in ``count`` real threads and return the results in index order.

    A worker's exception is re-raised to the caller rather than left as a
    missing entry. The runners this replaces collected into a dict and read
    misses back through a default, so two threads dying on a schema gap returned
    ``['', '']`` — which reads as a decision the code under test made, and was
    filed as a mutual-exclusion regression against what was a stale test fixture
    (souliane/teatree#4010). Only the lowest-indexed failure can be re-raised, so
    each worker's traceback is logged before that choice discards the rest.

    Ordering between the workers is the caller's business: each site builds its
    own ``threading.Barrier`` inside ``work``, because where the barrier sits
    relative to a stale read is the very thing some of them are pinning.
    """
    results: dict[int, T] = {}
    errors: dict[int, Exception] = {}

    def runner(idx: int) -> None:
        try:
            results[idx] = work(idx)
        except Exception as exc:
            logging.getLogger(__name__).debug("racing worker %d failed", idx, exc_info=exc)
            errors[idx] = exc
        finally:
            connections.close_all()

    threads = [threading.Thread(target=runner, args=(idx,)) for idx in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=timeout)
    if errors:
        raise errors[min(errors)]
    missing = [idx for idx in range(count) if idx not in results]
    if missing:
        msg = f"worker {missing[0]} did not finish within {timeout}s"
        raise TimeoutError(msg)
    return [results[idx] for idx in range(count)]
