"""Small argument-validation helpers for the ``t3 eval run`` command body.

Held apart from :mod:`teatree.cli.eval.app` (which is at its module-LOC cap) so
the command body stays thin: each resolves one CLI argument to its validated
domain value, or exits 2 (usage error) naming the valid choices.
"""

from pathlib import Path
from typing import cast

import typer
from claude_agent_sdk.types import EffortLevel

from teatree.eval.backends import API_BACKEND
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


def require_api_backend_for_fresh_run(*, backend: str, trials: int, models: str | None) -> None:
    """Reject fresh-run-only shapes unless the caller explicitly opts into api."""
    if trials == 1 and models is None:
        return
    if backend == API_BACKEND:
        return
    typer.echo(
        f"--trials/--models require a fresh metered run; pass --backend api instead of --backend {backend!r}.",
        err=True,
    )
    raise typer.Exit(code=2)


def reject_unsupported_run_output(
    *, output_format: str, transcript_html: Path | None, trials: int, models: str | None
) -> None:
    """Reject ``--format html`` and ``--transcript-html`` on the multi-trial/matrix shapes they don't support.

    ``--format html`` renders a SINGLE-trial ``list[ScenarioResult]`` and
    ``--transcript-html`` renders the per-TRIAL ``list[PassAtKResult]``; a
    ``--models`` matrix has neither, so both are usage errors there (and
    ``--format html`` is likewise rejected for ``--trials``). Exits 2 naming the
    fix rather than failing obscurely deeper in the run.
    """
    if output_format == "html" and (trials > 1 or models is not None):
        typer.echo("--format html is only supported for a single-trial run (not --trials/--models)", err=True)
        raise typer.Exit(code=2)
    if transcript_html is not None and models is not None:
        typer.echo(
            "--transcript-html is the per-TRIAL transcript report (a --trials run); a --models matrix "
            "has no per-trial transcript to render. Drop --models or drop --transcript-html.",
            err=True,
        )
        raise typer.Exit(code=2)
