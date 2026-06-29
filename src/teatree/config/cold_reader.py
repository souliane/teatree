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
    """
    db = db_path if db_path is not None else canonical_config_db(env=env)
    if not db.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        conn.execute("PRAGMA query_only=1")
        conn.execute("PRAGMA busy_timeout=100")
        row = conn.execute(
            "SELECT value FROM teatree_config_setting WHERE scope=? AND key=?",
            (scope, key),
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    if row is None:
        return None
    try:
        return json.loads(row[0])
    except (json.JSONDecodeError, TypeError):
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
