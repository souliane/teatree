"""Django-free stdlib WRITER for the canonical ``teatree_config_setting`` store (config-unify PR3).

The write-side twin of :mod:`teatree.config.cold_reader`. ``t3 <overlay> gate <name>
enable/disable`` is the orchestrator's guaranteed self-rescue and must stay Django-free
(no ORM, no ``manage.py`` subprocess) so it survives a wedged install. PR3 flips the
cold-hook gate READERS to the canonical DB; this module gives those same gates a
Django-free DB WRITE path, so the tier ``t3 gate`` writes IS the tier the flipped reader
reads. Without it a ``t3 gate disable`` (a TOML write) is SHADOWED by a seeded DB row,
and the never-lockout escape can never be lifted once ``t3 setup`` has seeded a row.

Targets the PRIMARY ``~/.local/share/teatree/db.sqlite3`` (never the per-worktree
isolated copy) by reusing :func:`cold_reader.canonical_config_db`. Every write fails SOFT
to ``False`` — a missing DB file (pre-setup cold state), a missing table (unmigrated DB),
a locked DB, or any sqlite error — so the caller falls back to the TOML write and the
self-rescue never raises.
"""

import json
import os
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from teatree.config.cold_reader import canonical_config_db

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


def write_setting(
    key: str,
    value: object,
    *,
    scope: str = _GLOBAL_SCOPE,
    env: Mapping[str, str] = os.environ,
    db_path: Path | None = None,
) -> bool:
    """UPSERT ``(scope, key)=value`` into the canonical DB; ``True`` on write, ``False`` if no DB/table.

    The Django-free stdlib write twin of :func:`cold_reader.read_setting`. The value is
    stored JSON-encoded (matching the ORM ``JSONField`` and the ``JSON_VALID`` check
    constraint), populating the NOT-NULL ``created_at``/``updated_at`` columns on insert.

    Returns ``False`` — so the caller (``t3 gate``) falls back to the TOML write — when the
    canonical DB FILE is absent (a fresh, pre-``t3 setup`` install) or the
    ``teatree_config_setting`` table is absent (a present-but-unmigrated DB), and on ANY
    sqlite error (a locked DB, a malformed file). The gate self-rescue therefore degrades to
    the TOML tier rather than raising — never-lockout.
    """
    db = db_path if db_path is not None else canonical_config_db(env=env)
    if not db.exists():
        return False
    payload = json.dumps(value)
    now = datetime.now(tz=UTC).strftime(_DJANGO_SQLITE_DATETIME)
    try:
        conn = sqlite3.connect(str(db), timeout=_BUSY_TIMEOUT_MS / 1000)
    except sqlite3.Error:
        return False
    try:
        conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        conn.execute(_UPSERT, (scope, key, payload, now, now))
        conn.commit()
    except sqlite3.Error:
        # A missing table (unmigrated DB) raises OperationalError; a locked DB raises
        # OperationalError too. Either way fail soft so the caller writes TOML instead.
        return False
    finally:
        conn.close()
    return True
