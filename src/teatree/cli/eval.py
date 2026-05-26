"""``t3 eval`` — behavioral eval harness commands."""

import os
import sys

import typer

from teatree.eval.discovery import discover_specs, find_spec
from teatree.eval.models import EvalSpec
from teatree.eval.report import ScenarioResult, evaluate, render_json, render_text
from teatree.eval.runner import ClaudePRunner

eval_app = typer.Typer(no_args_is_help=True, help="Behavioral eval harness.")


def _bootstrap_django() -> None:
    """Ensure Django is configured before overlay discovery runs.

    The overlay loader (``teatree.core.overlay_loader.get_all_overlays``)
    imports modules that touch Django models at import time, which raises
    ``ImproperlyConfigured`` in an unbootstrapped process. ``t3 eval`` is
    one of the few CLI surfaces that may run ahead of any other DB-touching
    command, so we bootstrap explicitly here rather than relying on a
    sibling command having warmed Django for us.
    """
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "teatree.settings")
    import django  # noqa: PLC0415
    from django.apps import apps  # noqa: PLC0415

    if not apps.ready:
        django.setup()


@eval_app.command("list")
def list_scenarios() -> None:
    """List discovered eval scenarios."""
    _bootstrap_django()
    specs = discover_specs()
    if not specs:
        typer.echo("(no scenarios discovered)")
        return
    for spec in specs:
        typer.echo(f"{spec.name}\t{spec.scenario}")


@eval_app.command("run")
def run(
    name: str | None = typer.Argument(None, help="Scenario name to run (omit to run all)."),
    output_format: str = typer.Option("text", "--format", help="Report format: text or json."),
    max_turns: int | None = typer.Option(
        None,
        "--max-turns",
        help="Override the scenario's max_turns (per-invocation).",
    ),
) -> None:
    """Run one scenario by name, or all scenarios when no name is given."""
    _bootstrap_django()
    specs = discover_specs() if name is None else [_require_spec(name)]
    runner = ClaudePRunner(max_turns_override=max_turns)
    results: list[ScenarioResult] = []
    for spec in specs:
        run_result = runner.run(spec)
        results.append(evaluate(spec, run_result))
    if output_format == "json":
        typer.echo(render_json(results))
    elif output_format == "text":
        typer.echo(render_text(results))
    else:
        typer.echo(f"unknown --format {output_format!r}; use 'text' or 'json'", err=True)
        raise typer.Exit(code=2)
    if any(not r.passed for r in results):
        sys.exit(1)


def _require_spec(name: str) -> EvalSpec:
    spec = find_spec(name)
    if spec is None:
        typer.echo(f"unknown scenario: {name!r}", err=True)
        available = ", ".join(s.name for s in discover_specs()) or "(none)"
        typer.echo(f"available scenarios: {available}", err=True)
        raise typer.Exit(code=2)
    return spec
