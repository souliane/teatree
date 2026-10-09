"""Generic per-session state-file IO shared by the hook router and its gates.

The router writes small newline-delimited state files under ``STATE_DIR``
(``<session>.reads``, ``<session>.pending``, …). These helpers are where that
dir is and the generic read/append primitives, factored into a bare sibling so
the router (at its module-health LOC cap) stays shrink-only and a gate sibling
or a separate hook process can reuse them without importing the router.
"""

import os
from pathlib import Path


def hook_state_dir() -> Path:
    """The per-session state dir every teatree hook process shares, as the environment names it now."""
    return Path(
        os.environ.get(
            "TEATREE_CLAUDE_STATUSLINE_STATE_DIR",
            os.environ.get("T3_HOOK_STATE_DIR", "/tmp/claude-statusline"),  # noqa: S108 — fixed agent-controlled path, not user input
        )
    )


def read_lines(path: Path) -> list[str]:
    """Non-empty stripped lines of *path*, or ``[]`` when it does not exist."""
    if not path.is_file():
        return []
    return [line for line in path.read_text(encoding="utf-8").strip().splitlines() if line]


def append_line(path: Path, line: str) -> None:
    """Append ``line`` (plus a newline) to *path*."""
    with path.open("a", encoding="utf-8") as f:
        f.write(f"{line}\n")
