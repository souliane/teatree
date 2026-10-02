"""Deterministic capture of representative ``t3`` command output as a fixture.

The CLI analog of ``core/dashboard_snapshot.py``: where that renders the admin
"screenshot", this renders the canonical front-door CLI output — ``t3 --help``
and ``t3 loop --help`` — to byte-stable markdown. It is the curated, always-fresh
complement to the EXHAUSTIVE auto-generated CLI reference (#67): a small embeddable
view of what the main commands print, not the full command tree.

Determinism is the whole contract (a flapping fixture reds CI). The output is a
pure function of the command tree captured through the #2599 render seam — pinned
width, no env-derived sizing, home-rooted dotfile defaults folded to ``~`` — so
``t3 --help`` rendered on a narrow CI runner and a wide local terminal are
byte-identical, and no timestamp/duration/PID/path can leak. The overlay surface
is pinned to ``t3-teatree`` so an installed overlay cannot make the front-door
command list vary by machine.

See: souliane/teatree#12
"""

from teatree.cli import app, register_overlay_commands
from teatree.cli.command_tree import render_help_blocks

# Each entry is the token list under ``t3``: ``[]`` → ``t3``, ``["loop"]`` → ``t3 loop``.
# Read-only, deterministic commands only (no rows, no clock, no leases) — their
# rendered help is the stable "what you see when you run it" the fixture captures.
_REPRESENTATIVE_PATHS: list[list[str]] = [[], ["loop"]]

_HEADER = (
    "# Representative CLI output\n\n"
    "Rendered `--help` output of the canonical `t3` commands, captured deterministically\n"
    "and drift-checked in CI so it stays an always-fresh fixture. This is the curated\n"
    "front-door complement to the exhaustive [CLI reference](../cli-reference.md);\n"
    "edit the CLI, not this file.\n\n"
)


def render_cli_output_snapshot() -> str:
    """Render the representative ``t3`` command outputs to deterministic markdown."""
    register_overlay_commands(allowlist={"t3-teatree"})
    return _HEADER + render_help_blocks(app, _REPRESENTATIVE_PATHS)
