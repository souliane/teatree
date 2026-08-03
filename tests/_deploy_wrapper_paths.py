"""Container-side paths read from ``deploy/t3`` rather than repeated in tests.

The wrapper is the only place that knows how a host root maps onto a container
path, so a test asserting a translated cwd needs the same value. Copying the
literal into each test file makes a second thing to update and lets the two
drift silently — a stale copy asserts a path the wrapper no longer produces.
"""

import re
from pathlib import Path

_WRAPPER = Path(__file__).resolve().parents[1] / "deploy" / "t3"

_SOURCE_DIR_PATTERN = r"^CONTAINER_SOURCE_DIR=(\S+)"
_WORKTREE_ROOT_PATTERN = r'"\$PHYSICAL_HOST_HOME/workspace/t3-workspaces:([^"]+)"'


def _capture(pattern: str, what: str) -> str:
    match = re.search(pattern, _WRAPPER.read_text(encoding="utf-8"), re.MULTILINE)
    if match is None:
        message = f"{_WRAPPER} no longer defines {what}"
        raise AssertionError(message)
    return match.group(1)


def container_source_dir() -> str:
    """The fixed container path the teatree source mount lands on."""
    return _capture(_SOURCE_DIR_PATTERN, "the container source dir")


def container_worktree_root() -> str:
    """The container side of the worktree-root bind mount."""
    return _capture(_WORKTREE_ROOT_PATTERN, "the worktree-root mount")
