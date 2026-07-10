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

_RUNNABLE_LOOP_STATUS = "enabled"


def canonical_config_db(env: Mapping[str, str] = os.environ, home: Path | None = None) -> Path:
    """Resolve the PRIMARY config DB path, never the per-worktree isolated one.

    `T3_CONFIG_DB` wins (an explicit test/override hook), then `XDG_DATA_HOME`,
    else `~/.local/share`. This intentionally ignores the worktree-isolation
    logic in `teatree.paths.resolve_data_dir` so a worktree checkout resolves to
    the same DB the installed `t3` uses.
    """
    override = env.get("T3_CONFIG_DB")
    if override:
        return Path(override)
    xdg = env.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else (home or Path.home()) / ".local" / "share"
    return base / "teatree" / "db.sqlite3"


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
        conn.execute("PRAGMA busy_timeout=100")
    except sqlite3.Error:
        conn.close()
        raise
    return conn


_QUERY_ERROR: object = object()
_RETRY_QUIESCENT_WAL: object = object()


def _read_once(db: Path, uri_parameters: str, query: str, parameters_bindings: tuple[object, ...]) -> object:
    """One read-only attempt through the `uri_parameters` open mode.

    Returns the fetched row (a tuple), `None` on a clean no-match, the
    `_QUERY_ERROR` sentinel on any sqlite error, or `_RETRY_QUIESCENT_WAL` when a
    read hit the quiescent-WAL `SQLITE_CANTOPEN` (the caller retries `immutable=1`).
    """
    try:
        conn = _open_readonly(db, uri_parameters)
    except sqlite3.Error:
        return _QUERY_ERROR
    try:
        return conn.execute(query, parameters_bindings).fetchone()
    except sqlite3.OperationalError as exc:
        if exc.sqlite_errorcode == sqlite3.SQLITE_CANTOPEN:
            return _RETRY_QUIESCENT_WAL
        return _QUERY_ERROR
    except sqlite3.Error:
        return _QUERY_ERROR
    finally:
        conn.close()


def _execute_readonly(db: Path, query: str, parameters_bindings: tuple[object, ...]) -> object:
    """Run a read-only single-row `query` with the quiescent-WAL fallback.

    Returns the fetched row (a tuple), `None` when the query ran cleanly but
    matched no row, or the `_QUERY_ERROR` sentinel on ANY sqlite error (a failed
    open, a locked DB, an absent table, a malformed query). Callers that fail
    open to a default collapse the sentinel to that default; `row_exists`
    distinguishes it from a clean empty result.

    The canonical DB is WAL-mode (`settings.SQLITE_WRITE_SERIALIZATION_OPTIONS`),
    so its file header is permanently WAL-format. When the DB is quiescent — no
    teatree process holding it, the standalone bash/statusline cold case this
    module exists for — its `-shm`/`-wal` sidecars are absent, and a `mode=ro`
    open then FAILS on first read with `SQLITE_CANTOPEN` (it can't recreate the
    `-shm`). So this tries `mode=ro` first (returns the WAL-current snapshot when
    a writer is live and the sidecars exist) and, only on that exact
    `SQLITE_CANTOPEN`, falls back to `immutable=1`, which opens the sidecar-less
    WAL-format file and reads the last-checkpointed value (correct, as no writer
    is active — see `teatree.paths._sqlite_snapshot`). A locked DB
    (`SQLITE_BUSY`), an absent table, and every other error keep resolving to the
    sentinel; `immutable=1` is the fallback ONLY for `SQLITE_CANTOPEN`, never a
    lock bypass. Shared by `fetch_one`, `loop_status`, and `row_exists` so every
    cold read runs through one WAL-aware sqlite path.
    """
    result = _read_once(db, "mode=ro", query, parameters_bindings)
    if result is _RETRY_QUIESCENT_WAL:
        result = _read_once(db, "immutable=1", query, parameters_bindings)
    return _QUERY_ERROR if result is _RETRY_QUIESCENT_WAL else result


def fetch_one(db: Path, query: str, parameters_bindings: tuple[object, ...]) -> tuple[object, ...] | None:
    """Read-only single-row `query`; fails open to `None` on any error or a missing row.

    The shared single-row fetch: `cold_reader._fetch_value_row` and `loop_status`
    both read one row through it, collapsing the `_QUERY_ERROR` sentinel to `None`.
    """
    row = _execute_readonly(db, query, parameters_bindings)
    return None if row is _QUERY_ERROR else cast("tuple[object, ...] | None", row)


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
        return default
    row = fetch_one(db, "SELECT status FROM teatree_loop_state WHERE name=?", (name,))
    if row is None:
        return default
    status = row[0]
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
