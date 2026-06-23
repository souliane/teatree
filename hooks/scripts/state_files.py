"""Generic per-session state-file IO shared by the hook router and its gates.

The router writes small newline-delimited state files under ``STATE_DIR``
(``<session>.reads``, ``<session>.pending``, …). These two helpers are the
generic read/append primitives, factored into a bare sibling so the router
(at its module-health LOC cap) stays shrink-only and a gate sibling can reuse
them without re-importing the router for trivial IO.
"""

from pathlib import Path


def read_lines(path: Path) -> list[str]:
    """Non-empty stripped lines of *path*, or ``[]`` when it does not exist."""
    if not path.is_file():
        return []
    return [line for line in path.read_text(encoding="utf-8").strip().splitlines() if line]


def append_line(path: Path, line: str) -> None:
    """Append ``line`` (plus a newline) to *path*."""
    with path.open("a", encoding="utf-8") as f:
        f.write(f"{line}\n")
