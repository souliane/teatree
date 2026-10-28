"""Django-free writer for the canonical config DB used by gate self-rescue.

The writer targets the primary control DB resolved by :func:`cold_reader.canonical_config_db`.
Its outcome retains the target path and SQLite failure so the CLI can report what happened.
"""

import json
import os
import sqlite3
import sys
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path

from teatree.config.cold_db import projection_dir_for
from teatree.config.cold_reader import canonical_config_db
from teatree.config.host_projection import GENERATION_KEY, ProjectionPublisher, ProjectionPublishError, next_generation
from teatree.config.value_visibility import MASKED, withholds_value
from teatree.config.write_authority import review_write_refusal
from teatree.db.boundary import ControlDbBoundary, DbBoundaryError

_GLOBAL_SCOPE = ""
_BUSY_TIMEOUT_MS = 30000

# Django stores aware datetimes in the sqlite backend as UTC strings in this exact
# format; reusing it keeps the NOT-NULL timestamp columns parseable by a later ORM read.
_DJANGO_SQLITE_DATETIME = "%Y-%m-%d %H:%M:%S.%f"

_UPSERT = (
    "INSERT INTO teatree_config_setting "
    "(scope, key, value, created_at, updated_at, seeded_by, written_by, seed_value) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
    "ON CONFLICT(scope, key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at, "
    "seeded_by=excluded.seeded_by, written_by=excluded.written_by, seed_value=excluded.seed_value"
)


class WriteResult(Enum):
    """Whether the write committed, lacked a usable DB, or failed after finding the table."""

    WROTE = "wrote"
    NO_DB_TIER = "no_db_tier"
    WRITE_FAILED = "write_failed"


@dataclass(frozen=True, slots=True)
class WriteOutcome:
    result: WriteResult
    db: Path
    detail: str = ""


def _table_present(conn: sqlite3.Connection) -> bool:
    """Whether the canonical ``teatree_config_setting`` table exists in *conn*'s database."""
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='teatree_config_setting'").fetchone()
    return row is not None


def _audit_table_present(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='teatree_config_setting_change'"
    ).fetchone()
    return row is not None


def write_setting(
    key: str,
    value: object,
    *,
    scope: str = _GLOBAL_SCOPE,
    env: Mapping[str, str] = os.environ,
    db_path: Path | None = None,
) -> WriteOutcome:
    """Write one setting and return the target DB plus the result; boundary violations raise."""
    db = db_path if db_path is not None else canonical_config_db(env=env)
    if reason := review_write_refusal(db, env=env):
        raise ValueError(reason)
    boundary = ControlDbBoundary(db)
    if not boundary.read_write_allowed:
        raise DbBoundaryError(boundary.refusal())
    if not db.exists():
        return WriteOutcome(WriteResult.NO_DB_TIER, db, "no file at this path")
    try:
        conn = sqlite3.connect(str(db), timeout=_BUSY_TIMEOUT_MS / 1000)
    except sqlite3.Error as error:
        return WriteOutcome(WriteResult.NO_DB_TIER, db, str(error))
    try:
        result, detail = _write_classified(conn, scope, key, value)
    finally:
        conn.close()
    if result is WriteResult.WROTE:
        _publish_projection(db, env)
    return WriteOutcome(result, db, detail)


def _bump_generation(conn: sqlite3.Connection) -> None:
    """Ratchet the projection generation inside the caller's open transaction.

    Same connection AND same transaction as the value write, so a generation the
    projection is published under can never label a value that did not commit — nor
    a committed value be published under a generation that did not move.
    """
    row = conn.execute(
        "SELECT value FROM teatree_config_setting WHERE scope=? AND key=?",
        (_GLOBAL_SCOPE, GENERATION_KEY),
    ).fetchone()
    try:
        current = json.loads(row[0]) if row is not None else None
    except (TypeError, ValueError):
        current = None
    _upsert(conn, _GLOBAL_SCOPE, GENERATION_KEY, next_generation(current))


def _publish_projection(db: Path, env: Mapping[str, str]) -> None:
    """Republish the host projection — the write seam IS the regeneration trigger.

    Loud on failure and never fatal: the settings write has already committed, so
    raising here would report a write that landed as a write that did not. The
    stderr line is what keeps a failed publication from becoming a silently stale
    projection, and ``t3 doctor check`` compares source to projection from inside the
    container where both are visible.
    """
    try:
        ProjectionPublisher(db, projection_dir_for(db, env)).publish()
    except (ProjectionPublishError, sqlite3.Error, OSError) as error:
        sys.stderr.write(f"WARNING: the host projection was not republished after a config write: {error}\n")


def _write_classified(conn: sqlite3.Connection, scope: str, key: str, value: object) -> tuple[WriteResult, str]:
    """Probe the table, then commit the value AND its generation bump as ONE transaction.

    Classifies an absent DB tier apart from a locked write. The two rows commit together
    or not at all: a value that committed while its generation bump was lost would be
    published under a generation the readers have already seen, so the projection carrying
    it reads FRESH while serving the pre-write value.
    """
    try:
        conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        table_present = _table_present(conn)
    except sqlite3.Error as error:
        return WriteResult.NO_DB_TIER, str(error)
    if not table_present:
        return WriteResult.NO_DB_TIER, "no teatree_config_setting table"
    try:
        conn.execute("BEGIN IMMEDIATE")
        previous = conn.execute(
            "SELECT value FROM teatree_config_setting WHERE scope=? AND key=?", (scope, key)
        ).fetchone()
        _upsert(conn, scope, key, value)
        if previous is None or previous[0] != json.dumps(value):
            if _audit_table_present(conn):
                now = datetime.now(tz=UTC).strftime(_DJANGO_SQLITE_DATETIME)
                old_value = json.loads(previous[0]) if previous else None
                old_json = (
                    json.dumps(MASKED)
                    if old_value is not None and withholds_value(key, old_value)
                    else previous[0]
                    if previous
                    else None
                )
                new_json = json.dumps(MASKED if withholds_value(key, value) else value)
                conn.execute("SAVEPOINT audit_row")
                try:
                    conn.execute(
                        "INSERT INTO teatree_config_setting_change "
                        "(scope, key, action, old_value, new_value, written_by, changed_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (scope, key, "set", old_json, new_json, "cold-writer", now),
                    )
                except sqlite3.Error as error:
                    conn.execute("ROLLBACK TO audit_row")
                    sys.stderr.write(f"WARNING: config audit row could not be written: {error}\n")
                finally:
                    conn.execute("RELEASE audit_row")
            else:
                sys.stderr.write("WARNING: config audit table is absent; rescue write has no audit row\n")
        if key != GENERATION_KEY:
            _bump_generation(conn)
        conn.commit()
    except sqlite3.Error as error:
        with suppress(sqlite3.Error):
            conn.rollback()
        return WriteResult.WRITE_FAILED, str(error)
    return WriteResult.WROTE, ""


def _upsert(conn: sqlite3.Connection, scope: str, key: str, value: object) -> None:
    now = datetime.now(tz=UTC).strftime(_DJANGO_SQLITE_DATETIME)
    conn.execute(_UPSERT, (scope, key, json.dumps(value), now, now, "", "cold-writer", None))
