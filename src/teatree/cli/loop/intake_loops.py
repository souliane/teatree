"""``t3 loop intake-loops`` — print the owner-intake loop names (#3632).

Prints the canonical :data:`teatree.loops.fleet_policy.OWNER_INTAKE_LOOPS` set,
one name per line, sorted. DB-free (a pure constant), so the deploy entrypoint's
``apply_fleet_loop_policy`` can read it cheaply to prune owner-intake loops from
the fleet DISABLED set — the single source of truth shared by the shell reseed and
the Python resolution tests, so the two can never drift.
"""

import typer

from teatree.loops.fleet_policy import OWNER_INTAKE_LOOPS


def intake_loops_command() -> None:
    """Print each owner-intake loop name (never fleet-masked off), one per line, sorted."""
    for name in sorted(OWNER_INTAKE_LOOPS):
        typer.echo(name)


__all__ = ["intake_loops_command"]
