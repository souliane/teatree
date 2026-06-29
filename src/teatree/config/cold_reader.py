"""Django-free stdlib reader for the DB-home `teatree_config_setting` store (config-unify PR1).

A zero-dependency cold path that reads the `ConfigSetting` override store
(`src/teatree/core/models/config_setting.py`) without booting Django — for the
bash/statusline path that cannot afford a Django import. It imports only the
standard library; in particular it does NOT import `teatree.paths`, whose
module-level `resolve_data_dir` would auto-isolate a worktree onto a sibling DB.

The deliberate inverse of `teatree.paths.resolve_data_dir`: this reader always
targets the PRIMARY `~/.local/share/teatree/db.sqlite3`, even from inside a git
worktree (a `.git` *file*). Config lives in one place — the installed `t3`'s DB —
and the statusline of a worktree session must read that same config, not an
isolated per-worktree copy. The ~5-line path computation is duplicated here
rather than imported; `tests/config/test_cold_reader.py` pins it equal to
`teatree.paths.TRUE_CANONICAL_DB` so the two can never drift.

Every read fails OPEN to `None` / the caller's default — a missing file, a
fresh install with no table, a locked DB, or a corrupt value never raises.
"""

import json
import os
import sqlite3
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast

_GLOBAL_SCOPE = ""


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


def _fetch_value_row(db: Path, scope: str, key: str) -> tuple[object, ...] | None:
    """Read the `(scope, key)` value row read-only, with the quiescent-WAL fallback.

    Fails open to `None` on any sqlite error or a missing row.
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
    (`SQLITE_BUSY`), an absent table, and every other error keep failing open to
    `None`; `immutable=1` is the fallback ONLY for `SQLITE_CANTOPEN`, never a lock
    bypass.
    """
    for parameters in ("mode=ro", "immutable=1"):
        try:
            conn = _open_readonly(db, parameters)
        except sqlite3.Error:
            return None
        try:
            return conn.execute(
                "SELECT value FROM teatree_config_setting WHERE scope=? AND key=?",
                (scope, key),
            ).fetchone()
        except sqlite3.OperationalError as exc:
            if parameters == "mode=ro" and exc.sqlite_errorcode == sqlite3.SQLITE_CANTOPEN:
                continue  # quiescent WAL: no sidecars → retry with immutable=1
            return None
        except sqlite3.Error:
            return None
        finally:
            conn.close()
    return None


def read_setting(
    key: str,
    *,
    scope: str = _GLOBAL_SCOPE,
    env: Mapping[str, str] = os.environ,
    db_path: Path | None = None,
) -> object | None:
    """Return the decoded value of `(scope, key)`, or `None` on any failure or absence.

    Fails open to `None` for every path: missing DB file, absent table (fresh
    install), locked DB (within `busy_timeout`), corrupt JSON, and a missing row.
    The open strategy (and the quiescent-WAL `immutable=1` fallback) lives in
    `_fetch_value_row`.
    """
    db = db_path if db_path is not None else canonical_config_db(env=env)
    if not db.exists():
        return None
    row = _fetch_value_row(db, scope, key)
    if row is None:
        return None
    raw = row[0]
    if not isinstance(raw, str | bytes | bytearray):
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _read_chain(name: str, scope_chain: Sequence[str], *, db_path: Path | None) -> object | None:
    """First scope in `scope_chain` with a stored value wins; `None` if none do."""
    for scope in scope_chain:
        value = read_setting(name, scope=scope, db_path=db_path)
        if value is not None:
            return value
    return None


def bool_setting(
    name: str,
    *,
    default: bool,
    scope_chain: Sequence[str] = (_GLOBAL_SCOPE,),
    db_path: Path | None = None,
) -> bool:
    """The stored value only when it is a real bool, else `default`.

    Mirrors `hooks/scripts/teatree_settings.section_bool_setting`: a quoted
    `"false"` is a `str`, not a bool, so it never disables a `default=True` flag.
    """
    value = _read_chain(name, scope_chain, db_path=db_path)
    return value if isinstance(value, bool) else default


def int_setting(
    name: str,
    *,
    default: int,
    minimum: int | None = None,
    scope_chain: Sequence[str] = (_GLOBAL_SCOPE,),
    db_path: Path | None = None,
) -> int:
    """The stored value only when it is a real int (not bool) at/above `minimum`, else `default`.

    A `bool` is rejected though it subclasses `int` (mirrors
    `teatree.config.settings._parse_strict_int`); a value below `minimum`
    degrades to `default` so the bound it encodes can't be mistyped away.

    Note: unlike the hot-path `settings._parse_strict_int` (which coerces a JSON
    string `"5"` → 5), this rejects a numeric string and falls back to `default`.
    That is intentional defense-in-depth, not a divergence to reconcile: the
    validated write path (`config_setting`) stores canonical JSON ints, so a
    string-typed numeric is unreachable here.
    """
    value = _read_chain(name, scope_chain, db_path=db_path)
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    if minimum is not None and value < minimum:
        return default
    return value


def str_setting(
    name: str,
    *,
    default: str,
    scope_chain: Sequence[str] = (_GLOBAL_SCOPE,),
    db_path: Path | None = None,
) -> str:
    """The stored value only when it is a real str, else `default` (no stringifying)."""
    value = _read_chain(name, scope_chain, db_path=db_path)
    return value if isinstance(value, str) else default


def list_setting(
    name: str,
    *,
    default: list[object],
    scope_chain: Sequence[str] = (_GLOBAL_SCOPE,),
    db_path: Path | None = None,
) -> list[object]:
    """The stored value only when it is a real list, else `default`."""
    value = _read_chain(name, scope_chain, db_path=db_path)
    if isinstance(value, list):
        return cast("list[object]", value)
    return default


def overlay_then_global(
    key: str,
    overlay: str,
    *,
    default: object | None = None,
    db_path: Path | None = None,
) -> object | None:
    """Read `scope=overlay` first, then global `scope=""`, else `default`.

    The cold-path twin of `resolution.py`'s two-tier layering — an overlay-scoped
    row beats a global one, exactly as a `[overlays.<name>]` value beats `[teatree]`.
    """
    value = _read_chain(key, (overlay, _GLOBAL_SCOPE), db_path=db_path)
    return value if value is not None else default


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        return 0
    value = read_setting(args[0])
    if value is not None:
        text = value if isinstance(value, str) else json.dumps(value)
        sys.stdout.write(text + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
