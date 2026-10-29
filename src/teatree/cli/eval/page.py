"""Render the current recorded behavioral eval results."""

from pathlib import Path

import typer

from teatree.eval.results_page import render_results_page
from teatree.utils.django_bootstrap import ensure_django


def page(out: Path | None = typer.Option(None, "--out", help="Write the results page to this path.")) -> None:
    ensure_django()
    body = render_results_page()
    if out is None:
        typer.echo(body, nl=False)
        return
    out.write_text(body)
    typer.echo(str(out))
