"""Shared interpretation of SQLite's quick integrity check."""

import sqlite3
from contextlib import closing

from django.db import DatabaseError
from django.db.backends.base.base import BaseDatabaseWrapper

_SQLITE_OK = "ok"


def quick_check_problems(connection: sqlite3.Connection | BaseDatabaseWrapper) -> list[str]:
    try:
        with closing(connection.cursor()) as cursor:
            cursor.execute("PRAGMA quick_check")
            rows = [str(row[0]) for row in cursor.fetchall()]
    except (sqlite3.DatabaseError, DatabaseError) as damaged:
        return [str(damaged)]
    return [] if rows == [_SQLITE_OK] else rows
