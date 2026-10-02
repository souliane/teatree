"""PostToolUse: record which repos this session has touched (#2663 extraction).

The concern is a self-contained one — classify the path a tool call named, resolve
it to a repo key, append it to the session's active-repo ledger — and it reached
nothing else in the router. It lives here because ``hook_router`` is a shrink-only
god-module: the module-health ratchet lets it only lose lines, so registering the
over-cap growth advisory had to be PAID for by moving a cohesive concern out. That
is the very rule the advisory exists to teach, applied to its own wiring.

The router internals this reads are back-imported INSIDE each function rather than
at module scope, so a test patching them on the router still steers the moved code
(the pattern ``cron_tracking`` already uses). The router keeps a one-line re-export
so ``router.handle_track_active_repo`` still resolves for ``_HANDLERS`` and tests.
"""

import os
import re
import subprocess  # noqa: S404 — stdlib subprocess for the trusted internal git call
import sys
from pathlib import Path
from typing import Final

# Alias both identities so the handler the router registers and a test patching a
# helper here operate on ONE module object.
sys.modules.setdefault("active_repo_tracking", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.active_repo_tracking", sys.modules[__name__])

_PATH_TOOLS: Final[frozenset[str]] = frozenset({"Grep", "Glob"})
_WORKTREE_PARTS: Final[int] = 2


def _extract_file_path(data: dict) -> str:
    from hooks.scripts.hook_router import _FILE_PATH_TOOLS  # noqa: PLC0415 — call-time back-import

    tool_name = data.get("tool_name", "")
    tool_input = data.get("tool_input", {})

    if tool_name in _FILE_PATH_TOOLS:
        return tool_input.get("file_path", "")
    if tool_name in _PATH_TOOLS:
        return tool_input.get("path", "")
    if tool_name == "Bash":
        match = re.search(r"/(Users|home)/[^ \"]+", tool_input.get("command", ""))
        return match.group() if match else ""
    return ""


def _resolve_repo_key(file_path: str, workspace: str) -> str | None:
    if not file_path.startswith(f"{workspace}/"):
        return None

    relative = file_path[len(workspace) + 1 :]
    parts = relative.split("/")
    first = parts[0]
    main_repo_dir = Path(workspace) / first

    if (main_repo_dir / ".git").is_dir():
        return first

    if len(parts) < _WORKTREE_PARTS:
        return None
    repo_in_wt = parts[1]
    wt_dir = main_repo_dir / repo_in_wt
    if not (wt_dir / ".git").exists():
        return None
    try:
        branch = subprocess.check_output(  # noqa: S603 — trusted internal subprocess; fixed argv, no shell
            ["git", "-C", str(wt_dir), "--no-optional-locks", "rev-parse", "--abbrev-ref", "HEAD"],  # noqa: S607 — trusted internal git invocation with a fixed argv
            text=True,
            timeout=3,
        ).strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return None
    return f"{branch}/{repo_in_wt}" if branch else None


def handle_track_active_repo(data: dict) -> None:
    """Track which repos the agent has touched during this session."""
    from hooks.scripts.hook_router import (  # noqa: PLC0415 — call-time back-import
        _append_line,
        _ensure_state_dir,
        _read_lines,
        _state_file,
    )

    session_id = data.get("session_id", "")
    if not session_id:
        return

    file_path = _extract_file_path(data)
    if not file_path:
        return

    workspace = os.environ.get("T3_WORKSPACE_DIR", str(Path.home() / "workspace"))
    repo_key = _resolve_repo_key(file_path, workspace)
    if repo_key is None:
        return

    _ensure_state_dir()
    active = _state_file(session_id, "active")
    if repo_key not in set(_read_lines(active)):
        _append_line(active, repo_key)

    # MR cache invalidation
    if data.get("tool_name") == "Bash":
        command = data.get("tool_input", {}).get("command", "")
        if "git push" in command or "glab mr" in command:
            mr_cache = _state_file(session_id, "mr_refreshed")
            if mr_cache.is_file():
                mr_cache.unlink()
