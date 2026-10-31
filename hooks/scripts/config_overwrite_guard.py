"""PreToolUse: block-config-overwrite (READ-BEFORE-OVERWRITE gate).

Refuses a blind destructive write to a tracked user config / dotfile. Two
surfaces fire when the agent has NOT read the file's current content this
session: (1) a ``Write`` or ``Edit`` that OVERWRITES an existing config/dotfile
(a ``dotfiles`` repo file, an XDG ``.config`` file) from
content the agent may have assumed rather than read; and (2) a
``git checkout`` / ``git restore`` that would restore a tracked config from a
committed version, discarding any uncommitted on-disk edits.

The on-disk content is treated as authoritative even when it diverges from the
committed version — reading it first (the existing ``<session>.reads`` capture
that ``handle_read_dedup`` already maintains) is the contract that clears the
gate. The decision core is :mod:`teatree.core.gates.config_overwrite_guard`;
this module supplies the ``was-read-this-session`` predicate, the per-call
escape, the kill-switch, and routes the deny through the router's shared
``_fail_open_or_deny`` chain (self-rescue allowlist + master fail-open +
circuit breaker all apply).

NEVER-LOCKOUT: a per-call ``[gate-escape:config_overwrite: <reason>]`` token (in the
Write content / Edit new_string / file_path, or the Bash command), the
``[teatree] config_overwrite_gate_enabled = false`` kill-switch
(``t3 <overlay> gate config-overwrite disable``), and the unconditional
fail-open chain all keep this gate from wedging a session.
"""

import contextlib
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from hooks.scripts.managed_repo import teatree_src_on_path

if TYPE_CHECKING:
    from teatree.core.gates.config_overwrite_guard import ConfigOverwriteFinding

# Alias the bare and ``hooks.scripts.`` identities so the handler the router
# registers and a test patching a helper here operate on ONE module object —
# the same pattern ``unknown_repo_push_gate`` uses.
sys.modules.setdefault("config_overwrite_guard", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.config_overwrite_guard", sys.modules[__name__])


def _config_overwrite_gate_enabled() -> bool:
    """Whether the read-before-overwrite gate is enabled (default True).

    Fails OPEN to enabled on a missing/broken config; an explicit ``false``
    (``[teatree] config_overwrite_gate_enabled = false``, flipped by
    ``t3 <overlay> gate config-overwrite disable``) is the one-line kill-switch.
    """
    from hooks.scripts.hook_router import _teatree_bool_setting  # noqa: PLC0415 deferred back-import

    return _teatree_bool_setting("config_overwrite_gate_enabled")


_READS_LINE_PARTS = 2


def _read_paths_this_session(session_id: str) -> set[str]:
    r"""Normalised set of paths the agent Read this session.

    Sourced from the existing ``<session>.reads`` capture
    (``handle_read_dedup`` writes one ``mtime\tpath`` line per Read). Each path
    is normalised through :func:`_normalise_path` so a Read of a home-dir dotfile
    matches a Write/restore of the same file expressed as a symlink, a
    relative path, or the symlink's resolved target.
    """
    from hooks.scripts.hook_router import STATE_DIR  # noqa: PLC0415 deferred back-import
    from hooks.scripts.state_files import read_lines  # noqa: PLC0415 deferred cold-hook import

    reads_file = STATE_DIR / f"{session_id}.reads"
    paths: set[str] = set()
    for line in read_lines(reads_file):
        parts = line.split("\t", 1)
        if len(parts) == _READS_LINE_PARTS:
            paths |= _normalise_path(parts[1])
    return paths


def _normalise_path(path: str) -> set[str]:
    """Every identity a path can present as, for read↔write matching.

    Returns the absolute path AND its symlink-resolved target (and the literal
    input). A config read via a home-dir symlink must satisfy a
    restore expressed as the dotfiles-repo target, and vice-versa, so both ends
    normalise to the same closure and the membership test is symmetric.
    """
    out: set[str] = {path}
    try:
        p = Path(path).expanduser()
        out.add(str(p))
        with contextlib.suppress(OSError):
            out.add(str(p.resolve()))
        with contextlib.suppress(OSError):
            out.add(str(p.absolute()))
    except (OSError, RuntimeError):
        pass
    return out


def _path_exists(file_path: str) -> bool:
    """True iff *file_path* resolves to an existing file (following symlinks)."""
    try:
        return Path(file_path).expanduser().exists()
    except (OSError, RuntimeError):
        return False


def _load_core():  # noqa: ANN202 — returns a lazily-imported handle; annotating would pull the type to module scope
    """Import the decision core, bootstrapping the sibling ``src/`` onto the path.

    Returns the core module, or ``None`` on any import failure — the caller then
    fails OPEN (allow), so a cold hook env without ``teatree`` never tracebacks.
    """
    try:
        with teatree_src_on_path():
            from teatree.core.gates import config_overwrite_guard as core  # noqa: PLC0415 — deferred: cold-hook import
    except Exception:  # noqa: BLE001 — a cold env without teatree fails OPEN, never tracebacks.
        return None
    return core


def _find_finding(
    core,  # noqa: ANN001 — untyped by design: a duck-typed handle passed positionally
    tool_name: str,
    tool_input: dict,
    was_read: Callable[[str], bool],
) -> "ConfigOverwriteFinding | None":
    """Resolve the blind-overwrite finding for the call's surface, or None.

    Split out of :func:`handle_block_config_overwrite` so each surface
    (``Write``/``Edit`` overwrite vs. ``Bash`` git-restore) reads as one branch
    and the handler stays under the complexity bar. A ``Write`` and an ``Edit``
    are the same risk — both mutate an existing config from content the agent
    may have assumed rather than read — so both route through the file-path
    branch.
    """
    if tool_name in {"Write", "Edit"}:
        file_path = tool_input.get("file_path", "")
        if isinstance(file_path, str) and file_path:
            return core.find_blind_write(
                file_path,
                exists=_path_exists(file_path),
                was_read=was_read(file_path),
            )
        return None
    command = tool_input.get("command", "")
    if isinstance(command, str) and command:
        return core.find_blind_git_restore(command, was_read=was_read)
    return None


def _gate_should_skip(data: dict) -> bool:
    """True iff a pre-check says this call is out of scope.

    Collapses the early-exit guards into one predicate to keep the handler small.
    """
    if data.get("tool_name", "") not in {"Write", "Edit", "Bash"}:
        return True
    if not _config_overwrite_gate_enabled():
        return True
    return not isinstance(data.get("tool_input", {}), dict)


def handle_block_config_overwrite(data: dict) -> bool:
    """Deny a blind overwrite/restore of a tracked user config / dotfile.

    Returns ``True`` (deny emitted) when the call is a blind config overwrite,
    ``False`` (allow) otherwise. Fail-open on every resolution failure so the
    gate never wedges a session; the deny routes through ``_fail_open_or_deny``.
    """
    from hooks.scripts.hook_router import _fail_open_or_deny  # noqa: PLC0415 deferred back-import

    if _gate_should_skip(data):
        return False
    core = _load_core()
    if core is None:
        return False  # cold env without teatree — fail OPEN, never traceback.

    session_id = data.get("session_id", "")
    read_paths = _read_paths_this_session(session_id) if session_id else set()

    def was_read(target: str) -> bool:
        return bool(_normalise_path(target) & read_paths)

    finding = _find_finding(core, data.get("tool_name", ""), data.get("tool_input", {}), was_read)
    if finding is None:
        return False
    return _fail_open_or_deny(data, core.deny_reason(finding), gate_id="config_overwrite")
