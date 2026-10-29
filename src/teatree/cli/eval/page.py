"""Render the current recorded behavioral eval results."""

from importlib import import_module
from pathlib import Path

import typer

from teatree.eval.artifact_redaction import write_artifact
from teatree.utils.django_bootstrap import ensure_django


def page(out: Path | None = typer.Option(None, "--out", help="Write the results page to this path.")) -> None:
    ensure_django()
    body = import_module("teatree.eval.results_page").render_results_page()
    if out is None:
        typer.echo(body, nl=False)
        return
    write_artifact(out, body)
    typer.echo(str(out))
