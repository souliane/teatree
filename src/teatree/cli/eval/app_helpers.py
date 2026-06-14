"""Small argument-validation helpers for the ``t3 eval run`` command body.

Held apart from :mod:`teatree.cli.eval.app` (which is at its module-LOC cap) so
the command body stays thin: each resolves one CLI argument to its validated
domain value, or exits 2 (usage error) naming the valid choices.
"""

from typing import cast

import typer
from claude_agent_sdk.types import EffortLevel

from teatree.eval.discovery import discover_specs, find_spec
from teatree.eval.model_variant import EFFORT_LEVELS
from teatree.eval.models import EvalSpec


def require_spec(name: str) -> EvalSpec:
    """Resolve a scenario by *name*, or exit 2 listing the available scenarios."""
    spec = find_spec(name)
    if spec is None:
        typer.echo(f"unknown scenario: {name!r}", err=True)
        available = ", ".join(s.name for s in discover_specs()) or "(none)"
        typer.echo(f"available scenarios: {available}", err=True)
        raise typer.Exit(code=2)
    return spec


def require_effort(effort: str) -> EffortLevel:
    """Validate ``--effort`` against the known levels, or exit 2 listing them."""
    if effort not in EFFORT_LEVELS:
        typer.echo(f"unknown --effort {effort!r}; known levels: {', '.join(EFFORT_LEVELS)}", err=True)
        raise typer.Exit(code=2)
    return cast("EffortLevel", effort)
