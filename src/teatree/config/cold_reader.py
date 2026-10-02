"""Django-free stdlib reader for the DB-home `teatree_config_setting` store (config-unify PR1).

A zero-dependency cold path that reads the `ConfigSetting` override store
(`src/teatree/core/models/config_setting.py`) without booting Django — for the
bash/statusline path that cannot afford a Django import. It imports only the
standard library (plus its siblings `cold_db`, the raw sqlite plumbing, and
`value_coercion`, the Django-free scalar coercers shared with the hot path); in
particular none of them import `teatree.paths`, whose module-level
`resolve_data_dir` would auto-isolate a worktree onto a sibling DB.

The typed `ConfigSetting` value readers (`read_setting` + the `bool`/`int`/`str`/
`list`/`mapping` coercions + the overlay→global chain) live here; the raw sqlite
plumbing (path resolution, read-only open, single-row fetch, loop-state status,
existence probe) lives in `cold_db` and is re-exported so every existing
`cold_reader.<name>` reference and `patch` target keeps resolving here.

Every read fails OPEN to `None` / the caller's default — a missing file, a
fresh install with no table, a locked DB, or a corrupt value never raises.
"""

import json
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from teatree.config import value_coercion
from teatree.config.cold_db import (
    canonical_config_db,
    canonical_projection,
    fetch_one_confirmed,
    loop_status,
    row_exists,
)

__all__ = [
    "SettingRead",
    "bool_setting",
    "canonical_config_db",
    "canonical_projection",
    "int_setting",
    "list_setting",
    "loop_status",
    "main",
    "mapping_setting",
    "overlay_then_global",
    "read_setting",
    "read_setting_confirmed",
    "row_exists",
    "str_setting",
]

_GLOBAL_SCOPE = ""


@dataclass(frozen=True)
class SettingRead:
    """One cold read's outcome: the decoded `value`, and whether the store could be READ.

    `readable` is `False` when sqlite itself errored, AND when a present-but-unreadable
    store's projection fallback has no answer for the key (:func:`_unreadable_db_read`) —
    that projection gap is indistinguishable from the corrupt store simply holding a value
    the projection never saw, so it is treated as unread rather than as a confirmed unset
    (#4205). Confirmed ABSENCE — no DB file at all, no row, an undecodable value — is
    `readable=True` with a `None` value: there was nothing to read, which is a legitimate
    answer rather than a failure.
    """

    value: object | None
    readable: bool


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
    `cold_db._execute_readonly`. A caller that must NOT fail open reads through
    `read_setting_confirmed`, of which this is the value half.
    """
    return read_setting_confirmed(key, scope=scope, env=env, db_path=db_path).value


def _absent_db_read(key: str, *, scope: str, env: Mapping[str, str], db_path: Path | None) -> "SettingRead":
    """The read when the canonical database file is not on this side of the volume.

    The ordinary host case: the database lives in a named volume only the container can
    open, so the value comes from the published projection rather than being None. An
    explicitly passed db_path never falls through — that caller named the file it means,
    and substituting a projection would answer a different question.
    """
    if db_path is not None:
        return SettingRead(None, readable=True)
    projection = canonical_projection(env=env)
    return SettingRead(projection.setting(key, scope=scope) if projection is not None else None, readable=True)


def _unreadable_db_read(key: str, *, scope: str, env: Mapping[str, str], db_path: Path | None) -> "SettingRead":
    """The read when the canonical database file is HERE but sqlite could not read it.

    The same projection tier :func:`_absent_db_read` uses, for the file that exists and
    answers nothing — the 0-byte stub the control-DB migration left at the old host path,
    or a database locked by a live writer. Keying the fall-through on absence alone left
    that stub with no tier at all, which is the shape #4197 closed in the shell (#4205).

    Unreadable is preserved when the projection cannot answer either — no projection at
    all, OR a trustworthy projection with no row for THIS key. The per-key case matters
    because `trustworthy` is generation-monotonic, not recency-checked: a projection whose
    publish failed after the corrupt DB's last write is still FRESH, so a missing key there
    is not evidence the key is unset — it is evidence the projection never saw it. Reporting
    `readable=True` for that gap is exactly the fail-open #4008 exists to close: it silently
    reopened it, letting a corrupt canonical DB collapse to "not configured" for
    `banned_terms` (measured: `resolve_banned_terms` moved from a raised
    `BannedTermsUnreadableError`, exit 2, to an allowed exit 0). This widens WHERE an answer
    may come from, never what counts as having read one.
    """
    if db_path is not None:
        return SettingRead(None, readable=False)
    projection = canonical_projection(env=env)
    if projection is None:
        return SettingRead(None, readable=False)
    value = projection.setting(key, scope=scope)
    return SettingRead(value, readable=value is not None)


def read_setting_confirmed(
    key: str,
    *,
    scope: str = _GLOBAL_SCOPE,
    env: Mapping[str, str] = os.environ,
    db_path: Path | None = None,
) -> SettingRead:
    """The `(scope, key)` read as a value PLUS whether the store was readable at all.

    The one read both views share, so a fail-open caller and a fail-closed one can never
    disagree about what the store said. Readability travels WITH the value rather than being
    a second probe a caller runs afterwards: under the lock contention that motivated this
    (#4008), a probe fired microseconds after the failed read can succeed and mask it.
    """
    db = db_path if db_path is not None else canonical_config_db(env=env)
    if not db.exists():
        # The ordinary host case: the database lives in a named volume only the container
        # can open, so fall through to the published projection rather than to None. An
        # explicitly passed db_path never does — that caller named the file it means, and
        # substituting a projection would answer a different question.
        return _absent_db_read(key, scope=scope, env=env, db_path=db_path)
    row, ran = fetch_one_confirmed(
        db,
        "SELECT value FROM teatree_config_setting WHERE scope=? AND key=?",
        (scope, key),
    )
    if not ran:
        return _unreadable_db_read(key, scope=scope, env=env, db_path=db_path)
    if row is None:
        return SettingRead(None, readable=True)
    raw = row[0]
    if not isinstance(raw, str | bytes | bytearray):
        return SettingRead(None, readable=True)
    try:
        return SettingRead(json.loads(raw), readable=True)
    except json.JSONDecodeError:
        return SettingRead(None, readable=True)


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

    Shares the strict coercion with the hot path via
    :func:`value_coercion.strict_int` but binds the cold policy
    `accept_numeric_str=False` — a `bool` (mistyped) and a numeric string `"5"`
    both degrade to `default` rather than raising into the cold read path. The
    numeric-string rejection is intentional defense-in-depth: the validated write
    path (`config_setting`) stores canonical JSON ints, so a string-typed numeric
    is unreachable here. A value below `minimum` degrades to `default` so the
    bound it encodes can't be mistyped away.
    """
    value = _read_chain(name, scope_chain, db_path=db_path)
    try:
        coerced = value_coercion.strict_int(value, accept_numeric_str=False)
    except (TypeError, ValueError):
        return default
    if minimum is not None and coerced < minimum:
        return default
    return coerced


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


def mapping_setting(
    name: str,
    *,
    scope_chain: Sequence[str] = (_GLOBAL_SCOPE,),
    db_path: Path | None = None,
) -> dict[str, object]:
    """The stored value as a typed mapping when it is a real dict, else an empty dict."""
    value = _read_chain(name, scope_chain, db_path=db_path)
    if isinstance(value, dict):
        return cast("dict[str, object]", value)
    return {}


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
