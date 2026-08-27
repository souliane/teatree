"""Retire the containerized-``t3`` shell alias during ``t3 setup`` (#3232).

A ``PATH`` executable reaches interactive AND non-interactive shells, so the
launcher (:mod:`teatree.cli.setup.docker_launcher`) supersedes the alias a
previous ``t3 setup`` wrote — and while both existed they could name different
checkouts, which is the split-brain the alias was papering over. Setup therefore
removes the managed block and installs no alias.

Only the fenced region goes; the rc files carry the operator's own functions.
Best-effort — an unwritable rc WARNs and never aborts setup.

Reaches only rc files this process can SEE, which on a containerized run is the
container's own. ``deploy/t3`` retires the same block on the host, where the
operator's rc actually lives.
"""

from collections.abc import Callable
from pathlib import Path

from teatree.docker.workflow import AliasRemoval, remove_alias_block

Echo = Callable[[str], None]


def retire_alias(*, echo: Echo, home: Path | None = None) -> None:
    """Remove the managed alias block from the shell rc files under *home*."""
    root = home if home is not None else Path.home()
    for rc_path in (root / ".bashrc", root / ".zshrc"):
        outcome = remove_alias_block(rc_path)
        if outcome is AliasRemoval.REMOVED:
            echo(f"OK    Removed the superseded containerized t3 alias from {rc_path} — the launcher replaces it.")
        elif outcome is AliasRemoval.UNWRITABLE:
            echo(
                f"WARN  Could not remove the containerized t3 alias from {rc_path} — delete the "
                f"`teatree docker t3 alias` block by hand; setup continues."
            )
