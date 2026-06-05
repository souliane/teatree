"""``t3 eval negative-control`` — the harness self-test command (teatree#1160)."""

import sys

import typer

from teatree.eval.negative_control import render_outcome, run_negative_control

_VALID_FORMATS = ("text", "json")


def _bootstrap_django() -> None:
    import django  # noqa: PLC0415
    from django.apps import apps  # noqa: PLC0415

    if not apps.ready:
        django.setup()


def negative_control(
    output_format: str = typer.Option("text", "--format", help="Report format: text or json."),
) -> None:
    """Self-test the harness: plant a known violation and assert it is caught (token-free)."""
    _bootstrap_django()
    if output_format not in _VALID_FORMATS:
        typer.echo(f"unknown --format {output_format!r}; use 'text' or 'json'", err=True)
        raise typer.Exit(code=2)
    outcome = run_negative_control()
    typer.echo(render_outcome(outcome, as_json=output_format == "json"))
    if not outcome.caught:
        sys.exit(1)
