"""Shared helpers for tests that migrate a private, file-backed SQLite alias.

Migrates to HEAD without touching the shared ``default`` test database
(#2915). ``tests/`` is a package and sits on ``pythonpath`` (see
``pyproject.toml``), so both ``tests/teatree_core/conftest.py`` (not itself
a package) and top-level test modules can import from here without
duplicating the router or the alias register/teardown boilerplate.
"""

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
