"""``t3 eval negative-control`` — the harness self-test command (teatree#1160)."""

import sys

import typer

from teatree.cli._format_opts import require_valid_format
from teatree.eval.negative_control import render_outcome, run_negative_control
from teatree.utils.django_bootstrap import ensure_django


def negative_control(
    output_format: str = typer.Option("text", "--format", help="Report format: text or json."),
) -> None:
    """Self-test the harness: plant a known violation and assert it is caught (token-free)."""
    ensure_django()
    require_valid_format(output_format)
    outcome = run_negative_control()
    typer.echo(render_outcome(outcome, as_json=output_format == "json"))
    if not outcome.caught:
        sys.exit(1)
