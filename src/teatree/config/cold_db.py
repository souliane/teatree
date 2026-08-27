"""Django-free stdlib sqlite plumbing for the canonical DB-home store.

The raw read layer split out of :mod:`teatree.config.cold_reader` (the
module-health function-count bar): resolving the PRIMARY config DB path, opening
it read-only with the WAL-aware fallback, the shared single-row fetch, and the
two non-config-setting reads built straight on that plumbing — the loop-state
status and the generic existence probe. :mod:`teatree.config.cold_reader` layers
the typed `ConfigSetting` value readers on top of this module.

Imports only the standard library; in particular it does NOT import
`teatree.paths`, whose module-level `resolve_data_dir` would auto-isolate a
worktree onto a sibling DB. The deliberate inverse of that resolver:
`canonical_config_db` always targets the PRIMARY
`~/.local/share/teatree/db.sqlite3`, even from inside a git worktree (a `.git`
*file*) — config lives in one place, the installed `t3`'s DB. The ~5-line path
computation is duplicated here rather than imported;
`tests/config/test_cold_reader.py` pins it equal to
`teatree.paths.TRUE_CANONICAL_DB` so the two can never drift.

Every read fails OPEN to `None` / the caller's default — a missing file, a fresh
install with no table, a locked DB, or a corrupt value never raises.
"""

import os
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import cast

from teatree.config.host_projection import HostProjection, ProjectionReader, warn_once
from teatree.paths import ControlDb

_RUNNABLE_LOOP_STATUS = "enabled"

#: How long a cold read waits for a contended lock before reporting the store UNREADABLE.
#: The canonical DB runs `journal_mode=TRUNCATE` (`settings.SQLITE_WRITE_SERIALIZATION_OPTIONS`
#: dropped WAL), so a committing writer holds EXCLUSIVE and a reader genuinely blocks — the
#: opposite of WAL, which is what the 100ms this replaces was sized for. Matches the cold
#: WRITER's own wait on the same file (`cold_writer._BUSY_TIMEOUT_MS`) and stays far under the
#: ORM tier's 30s, so a genuinely stuck lock is still reported rather than waited out.
_BUSY_TIMEOUT_MS = 2000


def canonical_data_dir(env: Mapping[str, str] = os.environ, home: Path | None = None) -> Path:
    """The PRIMARY teatree DATA dir — the bind-mounted tree, NOT the control DB's parent.

    The control DB moved into a named volume, so `<canonical_config_db>.parent` is a
    filesystem the host cannot see. Every caller that wanted the data dir (the
    availability override, the presence heartbeat, backups, the handover mirror)
    resolves it here instead, and keeps resolving the same host-visible directory it
    always did.
    """
    return ControlDb(env, home).primary_data_dir()


def projection_dir_for(db: Path, env: Mapping[str, str] = os.environ) -> Path:
    """Where *db*'s host projection is published.

    The canonical DB projects into the bind-mounted data dir — the whole point, since
    that is the only side the host hooks can read. Any other database (a test fixture,
    a per-worktree isolated copy) has no volume boundary to cross, so its projection
    sits beside it.
    """
    return canonical_data_dir(env=env) if db == canonical_config_db(env=env) else db.parent


def canonical_projection(env: Mapping[str, str] = os.environ) -> HostProjection | None:
    """The published host projection, or `None` with ONE loud advisory when untrustworthy.

    The cold readers' fallback when the canonical DB is unreachable — which, on a host,
    is the ordinary case now. An absent, malformed, schema-shifted or generation-
    regressed projection resolves to `None` so the caller takes its compiled-in default
    exactly as before, but never SILENTLY: the advisory names the file and says the
    default is what is now in force (#3499's whole failure was that silence).
    """
    read = ProjectionReader(canonical_data_dir(env=env)).read()
    warn_once(read.advisory)
    return read.projection if read.trustworthy else None


def canonical_config_db(env: Mapping[str, str] = os.environ, home: Path | None = None) -> Path:
    """Resolve the PRIMARY config DB path, never the per-worktree isolated one.

    Delegates to the ONE resolution seam (:meth:`teatree.paths.ControlDb.primary`,
    #3514) rather than re-implementing the `T3_CONFIG_DB` → `XDG_DATA_HOME` →
    `~/.local/share` precedence: two copies of that precedence is how subcommands
    came to disagree about which database they were talking to. It intentionally
    takes the PRIMARY branch of the seam, so a worktree checkout resolves the same
    DB the installed `t3` uses.

    An absent `home` is left for the seam to default LAZILY, never pre-resolved here:
    an explicit `T3_CONFIG_DB` fixes the answer before any home lookup, so a cold read
    under that override must not touch the home tree at all.
    """
    return ControlDb(env, home).primary()


def _open_readonly(db: Path, parameters: str) -> sqlite3.Connection:
    """Open `db` for a read-only query, with the shared read PRAGMA setup.

    `parameters` is the URI query — `mode=ro` (the live-writer fast path) or
    `immutable=1` (the quiescent-WAL fallback). The URI is built via
    `Path.as_uri()`, which percent-encodes URI-special path characters (space,
    `%`, `?`, `#`), so an exotic `T3_CONFIG_DB` path can't malform it into a
    silent fail-open; `.absolute()` satisfies `as_uri`'s absolute-path
    requirement (every config-DB path is absolute in practice). On a PRAGMA
    failure the connection is closed before the error propagates, so a failed
    open never strands an open handle.
    """
    conn = sqlite3.connect(f"{db.absolute().as_uri()}?{parameters}", uri=True)
    try:
        conn.execute("PRAGMA query_only=1")
        conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
    except sqlite3.Error:
        conn.close()
        raise
    return conn


_QUERY_ERROR: object = object()
_RETRY_QUIESCENT_WAL: object = object()


def _read_once(
    db: Path, uri_parameters: str, query: str, parameters_bindings: tuple[object, ...], *, many: bool
) -> object:
    """One read-only attempt through the `uri_parameters` open mode.

    Returns the fetched row (a tuple; a list of tuples when `many`), `None` on a
    clean no-match, the `_QUERY_ERROR` sentinel on any sqlite error, or
    `_RETRY_QUIESCENT_WAL` when a read hit the quiescent-WAL `SQLITE_CANTOPEN`
    (the caller retries `immutable=1`).
    """
    try:
        conn = _open_readonly(db, uri_parameters)
    except sqlite3.Error:
        return _QUERY_ERROR
    try:
        cursor = conn.execute(query, parameters_bindings)
        return cursor.fetchall() if many else cursor.fetchone()
    except sqlite3.OperationalError as exc:
        if exc.sqlite_errorcode == sqlite3.SQLITE_CANTOPEN:
            return _RETRY_QUIESCENT_WAL
        return _QUERY_ERROR
    except sqlite3.Error:
        return _QUERY_ERROR
    finally:
        conn.close()


def _execute_readonly(db: Path, query: str, parameters_bindings: tuple[object, ...], *, many: bool = False) -> object:
    """Run a read-only single-row `query` with the quiescent-WAL fallback.

    Returns the fetched row (a tuple), `None` when the query ran cleanly but
    matched no row, or the `_QUERY_ERROR` sentinel on ANY sqlite error (a failed
    open, a locked DB, an absent table, a malformed query). Callers that fail
    open to a default collapse the sentinel to that default; `row_exists`
    distinguishes it from a clean empty result.

    The `immutable=1` fallback is for a WAL-FORMAT file. The canonical DB is no
    longer one — `settings.SQLITE_WRITE_SERIALIZATION_OPTIONS` dropped WAL for
    `journal_mode=TRUNCATE` — but the header survives on any file written before
    that, and on a fixture that asks for WAL. Such a file is unreadable through
    `mode=ro` while quiescent (no teatree process holding it, the standalone
    bash/statusline cold case this module exists for): its `-shm`/`-wal` sidecars
    are absent and the open FAILS on first read with `SQLITE_CANTOPEN`, unable to
    recreate the `-shm`. So this tries `mode=ro` first and, only on that exact
    `SQLITE_CANTOPEN`, falls back to `immutable=1`, which opens the sidecar-less
    file and reads the last-checkpointed value (correct, as no writer is active —
    see `teatree.paths._sqlite_snapshot`). A lock is waited out for
    `_BUSY_TIMEOUT_MS` first and then resolves to the sentinel, like an absent
    table and every other error; `immutable=1` is the fallback ONLY for
    `SQLITE_CANTOPEN`, never a lock bypass. Shared by `fetch_one`, `loop_status`
    and `row_exists`, so every cold read runs through one sqlite path.
    """
    result = _read_once(db, "mode=ro", query, parameters_bindings, many=many)
    if result is _RETRY_QUIESCENT_WAL:
        result = _read_once(db, "immutable=1", query, parameters_bindings, many=many)
    return _QUERY_ERROR if result is _RETRY_QUIESCENT_WAL else result


def fetch_one(db: Path, query: str, parameters_bindings: tuple[object, ...]) -> tuple[object, ...] | None:
    """Read-only single-row `query`; fails open to `None` on any error or a missing row.

    The shared single-row fetch, collapsing the `_QUERY_ERROR` sentinel to `None`.
    """
    return fetch_one_confirmed(db, query, parameters_bindings)[0]


def fetch_one_confirmed(
    db: Path, query: str, parameters_bindings: tuple[object, ...]
) -> tuple[tuple[object, ...] | None, bool]:
    """`(row, ran)` — the row, plus whether the read actually RAN.

    `fetch_one`'s confirming sibling, for a caller that must tell a clean "no such row" from a
    store it could not read at all (a locked DB, a corrupt file, an absent table). `fetch_one`
    collapses both to `None`, which is right for a fail-open caller and exactly wrong for a
    security gate: the banned-terms scanner read "no `banned_terms` row" out of a DB that was
    merely busy, and opened (#4008). `ran` is `False` only when sqlite itself errored.
    """
    row = _execute_readonly(db, query, parameters_bindings)
    if row is _QUERY_ERROR:
        return (None, False)
    return (cast("tuple[object, ...] | None", row), True)


def fetch_all(db: Path, query: str, parameters_bindings: tuple[object, ...]) -> list[tuple[object, ...]]:
    """Read-only multi-row `query`; fails open to `[]` on any error.

    The multi-row sibling of `fetch_one`, riding the same WAL-aware path. The cold
    posture resolver needs it for the active schedule's slots — the one place a cold
    read is a set rather than a scalar.
    """
    rows = _execute_readonly(db, query, parameters_bindings, many=True)
    return [] if rows is _QUERY_ERROR or rows is None else cast("list[tuple[object, ...]]", rows)


def loop_status(
    name: str,
    *,
    default: str = _RUNNABLE_LOOP_STATUS,
    env: Mapping[str, str] = os.environ,
    db_path: Path | None = None,
) -> str:
    """Durable status of loop `name` from `teatree_loop_state`, or `default` on absence/failure.

    The Django-free cold twin of `LoopState.objects.status_of`: an absent row —
    or an unreadable DB — resolves to the runnable `enabled` default, exactly as
    the model manager's absent-row fall-through does (there is no seeded-defaults
    migration; an empty table means every loop runs). Fails OPEN to `default` for
    every path — missing DB file, absent table (fresh install), locked DB, a
    non-str status — so the caller never suppresses on an unreadable control
    plane. Reuses `canonical_config_db` + the WAL-aware `fetch_one` so it targets
    the same PRIMARY `~/.local/share/teatree/db.sqlite3` the installed `t3` writes,
    even from inside a worktree.
    """
    db = db_path if db_path is not None else canonical_config_db(env=env)
    if not db.exists():
        return _projected_loop_status(name, default=default, env=env) if db_path is None else default
    row = fetch_one(db, "SELECT status FROM teatree_loop_state WHERE name=?", (name,))
    if row is None:
        return default
    status = row[0]
    return status if isinstance(status, str) and status else default


def _projected_loop_status(name: str, *, default: str, env: Mapping[str, str]) -> str:
    projection = canonical_projection(env=env)
    status = projection.loop_status(name) if projection is not None else None
    return status if isinstance(status, str) and status else default


def row_exists(
    query: str,
    parameters_bindings: tuple[object, ...] = (),
    *,
    on_error: bool,
    env: Mapping[str, str] = os.environ,
    db_path: Path | None = None,
) -> bool:
    """Whether `query` (a `SELECT … LIMIT 1` existence probe) returns any row.

    Django-free existence check for the cold hot-path (e.g. the UserPromptSubmit
    inject handlers deciding whether to boot Django at all). Semantics are
    "confirmed": a DB that opens and runs the query cleanly returns `True` iff a
    row matched, else `False`. Anything that leaves the answer UNCONFIRMED — a
    missing DB file, a locked DB, an absent table, a malformed query — resolves
    to `on_error`. A hot-path caller passes `on_error=True` to FAIL OPEN (treat
    an unconfirmable probe as "assume there is work") so a pending row is never
    silently dropped and the caller falls back to booting Django + the real ORM
    query. Reuses the shared WAL-aware `_execute_readonly` path.
    """
    db = db_path if db_path is not None else canonical_config_db(env=env)
    if not db.exists():
        return on_error
    row = _execute_readonly(db, query, parameters_bindings)
    if row is _QUERY_ERROR:
        return on_error
    return row is not None
