"""Django-free stdlib WRITER for the canonical ``teatree_config_setting`` store (config-unify PR3).

The write-side twin of :mod:`teatree.config.cold_reader`. ``t3 <overlay> gate <name>
enable/disable`` is the orchestrator's guaranteed self-rescue and must stay Django-free
(no ORM, no ``manage.py`` subprocess) so it survives a wedged install. PR3 flips the
cold-hook gate READERS to the canonical DB; this module gives those same gates a
Django-free DB WRITE path, so the tier ``t3 teatree gate`` writes IS the tier the flipped
reader reads. Without it a ``t3 teatree gate disable`` (a TOML write) is SHADOWED by a seeded DB row,
and the never-lockout escape can never be lifted once ``t3 setup`` has seeded a row.

Targets the PRIMARY ``~/.local/share/teatree/db.sqlite3`` (never the per-worktree
isolated copy) by reusing :func:`cold_reader.canonical_config_db`. The write never raises —
it returns a :class:`WriteResult` classifying the outcome so the caller (``t3 teatree gate``) can
tell a genuinely absent DB tier (fall back to the TOML write) apart from a present-but-locked
DB (the DB row stays authoritative, so a TOML write would be a dead, shadowed row).
"""

import json
import os
import sqlite3
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path

from teatree.config.cold_db import projection_dir_for
from teatree.config.cold_reader import canonical_config_db
from teatree.config.host_projection import GENERATION_KEY, ProjectionPublisher, ProjectionPublishError, next_generation
from teatree.db.boundary import ControlDbBoundary, DbBoundaryError

_GLOBAL_SCOPE = ""
_BUSY_TIMEOUT_MS = 2000

# Django stores aware datetimes in the sqlite backend as UTC strings in this exact
# format; reusing it keeps the NOT-NULL timestamp columns parseable by a later ORM read.
_DJANGO_SQLITE_DATETIME = "%Y-%m-%d %H:%M:%S.%f"

_UPSERT = (
    "INSERT INTO teatree_config_setting (scope, key, value, created_at, updated_at) "
    "VALUES (?, ?, ?, ?, ?) "
    "ON CONFLICT(scope, key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at"
)


class WriteResult(Enum):
    """Outcome of a Django-free cold write to the canonical config DB.

    The caller (``t3 teatree gate``) must distinguish three states:

    - ``WROTE`` — the value committed to the canonical DB; the DB tier is authoritative.
    - ``NO_DB_TIER`` — no usable canonical DB: an absent file (a fresh, pre-``t3 setup``
        install), a present-but-unmigrated DB (no ``teatree_config_setting`` table), or a
        corrupt/unopenable file. There is no config file to fall back to, so the write could
        not land anywhere; the caller surfaces this (run ``t3 setup`` to create the DB).
    - ``WRITE_FAILED`` — the DB file AND the table are present, but the write itself failed
        (a locked DB — ``SQLITE_BUSY`` after the busy-timeout). The DB is the only tier, so the
        caller surfaces the failure (read-back-verify) rather than silently losing the write.

    The discriminator between ``NO_DB_TIER`` and ``WRITE_FAILED`` is an explicit
    table-existence probe, NOT a sqlite error-code classification — so a locked write is never
    mistaken for a missing tier, and vice versa.
    """

    WROTE = "wrote"
    NO_DB_TIER = "no_db_tier"
    WRITE_FAILED = "write_failed"


def _table_present(conn: sqlite3.Connection) -> bool:
    """Whether the canonical ``teatree_config_setting`` table exists in *conn*'s database."""
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='teatree_config_setting'").fetchone()
    return row is not None


def write_setting(
    key: str,
    value: object,
    *,
    scope: str = _GLOBAL_SCOPE,
    env: Mapping[str, str] = os.environ,
    db_path: Path | None = None,
) -> WriteResult:
    """UPSERT ``(scope, key)=value`` into the canonical DB; classify the outcome for ``t3 teatree gate``.

    The Django-free stdlib write twin of :func:`cold_reader.read_setting`. The value is
    stored JSON-encoded (matching the ORM ``JSONField`` and the ``JSON_VALID`` check
    constraint), populating the NOT-NULL ``created_at``/``updated_at`` columns on insert.

    Returns a :class:`WriteResult` for every DATA condition (never raises on one):
    ``NO_DB_TIER`` when there is no usable canonical DB to write (absent file,
    unmigrated/corrupt, no table) so the caller writes TOML; ``WRITE_FAILED`` when the table
    is present but the write was blocked by a lock so the caller must NOT mask the live DB
    row with a dead TOML write; ``WROTE`` on a committed write.

    The one TOPOLOGY fault does raise :class:`DbBoundaryError`: this is the only writer in
    the project that reaches the canonical DB outside Django, so the guarded backend cannot
    cover it, and degrading a cross-VM-boundary write to a ``WriteResult`` would hand the
    caller a silent fallback exactly where a silent fallback corrupts pages.
    """
    db = db_path if db_path is not None else canonical_config_db(env=env)
    boundary = ControlDbBoundary(db)
    if not boundary.read_write_allowed:
        raise DbBoundaryError(boundary.refusal())
    if not db.exists():
        return WriteResult.NO_DB_TIER
    try:
        conn = sqlite3.connect(str(db), timeout=_BUSY_TIMEOUT_MS / 1000)
    except sqlite3.Error:
        return WriteResult.NO_DB_TIER
    try:
        result = _upsert_classified(conn, scope, key, value)
        if result is WriteResult.WROTE and key != GENERATION_KEY:
            _bump_generation(conn)
    finally:
        conn.close()
    if result is WriteResult.WROTE:
        _publish_projection(db, env)
    return result


def _bump_generation(conn: sqlite3.Connection) -> None:
    """Ratchet the projection generation on the SAME connection that just wrote.

    Same connection, so the counter can never disagree with the write it labels, and
    no second handle is opened on a database whose whole point is one writer.
    """
    row = conn.execute(
        "SELECT value FROM teatree_config_setting WHERE scope=? AND key=?",
        (_GLOBAL_SCOPE, GENERATION_KEY),
    ).fetchone()
    try:
        current = json.loads(row[0]) if row is not None else None
    except (TypeError, ValueError):
        current = None
    _upsert_classified(conn, _GLOBAL_SCOPE, GENERATION_KEY, next_generation(current))


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


def _upsert_classified(conn: sqlite3.Connection, scope: str, key: str, value: object) -> WriteResult:
    """Probe the table, then UPSERT — classifying an absent DB tier apart from a locked write."""
    payload = json.dumps(value)
    now = datetime.now(tz=UTC).strftime(_DJANGO_SQLITE_DATETIME)
    try:
        conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        table_present = _table_present(conn)
    except sqlite3.Error:
        # The schema can't even be probed -> a corrupt/unusable file. The cold reader fails
        # open to None on it too, so TOML is what gets read: treat it as no DB tier.
        return WriteResult.NO_DB_TIER
    if not table_present:
        return WriteResult.NO_DB_TIER  # a present-but-unmigrated DB -> caller writes TOML
    try:
        conn.execute(_UPSERT, (scope, key, payload, now, now))
        conn.commit()
    except sqlite3.Error:
        # The table is present but the write failed -- a locked DB (SQLITE_BUSY after the
        # busy-timeout). The DB row stays authoritative, so the caller must NOT write a dead,
        # shadowed TOML row; it surfaces the failure via read-back-verify instead.
        return WriteResult.WRITE_FAILED
    return WriteResult.WROTE
