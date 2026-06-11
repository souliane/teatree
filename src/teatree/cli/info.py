"""Top-level info-style commands: ``info``, ``startoverlay``, ``docs``."""

import sys
from pathlib import Path
from typing import Annotated

import typer

info_app = typer.Typer(
    no_args_is_help=False,
    invoke_without_command=True,
    help="Installation info (bare) and read-only per-ticket artifact discovery.",
)


@info_app.callback()
def info(ctx: typer.Context) -> None:
    """Show t3 installation, teatree/overlay sources, and editable status.

    The bare ``t3 info`` (no subcommand) prints the installation report; a
    subcommand (e.g. ``t3 info artifacts <ticket>``) runs instead.
    """
    if ctx.invoked_subcommand is not None:
        return
    from teatree.cli.doctor import DoctorService  # noqa: PLC0415

    DoctorService.show_info()


@info_app.command()
def artifacts(
    ticket_id: int,
    *,
    output_format: Annotated[
        str,
        typer.Option("--format", help="text (default) | json"),
    ] = "text",
) -> None:
    """Locate every artifact for a ticket: stack + ports, plans, run artifacts, E2E evidence.

    Read-only "find our eggs" aggregation over a ticket's existing rows —
    where its worktrees/stacks live (on-disk path, db_name, host ports, state),
    its PlanArtifact rows, each Task's ``result_artifact_path``, and its
    E2eMandatoryRun evidence (spec + posted video/comment URL).

    ``--format`` validation, ticket resolution, and rendering all live in the
    ``info`` management command this delegates to (the ORM-touching seam).
    """
    from teatree.utils.django_bootstrap import ensure_django  # noqa: PLC0415

    ensure_django()
    from django.core.management import call_command  # noqa: PLC0415

    call_command("info", "artifacts", ticket_id, output_format=output_format)


def startoverlay(
    project_name: str,
    destination: Path,
    *,
    overlay_app: str = typer.Option("t3_overlay", "--overlay-app", help="Name of the overlay Django app"),
    project_package: str | None = typer.Option(
        None,
        "--project-package",
        help="Project package name (default: derived from project name)",
    ),
) -> None:
    """Create a new TeaTree overlay package."""
    from teatree.overlay_init.generator import OverlayScaffolder  # noqa: PLC0415

    project_root = destination / project_name
    if project_root.exists():
        typer.echo(f"Destination already exists: {project_root}")
        raise typer.Exit(code=1)

    package_name = project_package or project_name.replace("-", "_").replace("t3_", "")
    scaffolder = OverlayScaffolder(project_root, overlay_app, package_name)
    scaffolder.scaffold(project_name)
    typer.echo(str(project_root))


def docs(
    host: str = typer.Option("127.0.0.1", help="Host to bind to"),
    port: int = typer.Option(8888, help="Port to serve on"),
) -> None:
    """Serve the project documentation with mkdocs.

    Requires the ``docs`` dependency group: ``uv sync --group docs``
    """
    from teatree.cli import _find_project_root  # noqa: PLC0415
    from teatree.utils.run import run_streamed  # noqa: PLC0415

    project_root = _find_project_root()
    mkdocs_yml = project_root / "mkdocs.yml"
    if not mkdocs_yml.exists():
        typer.echo(f"No mkdocs.yml found in {project_root}")
        raise typer.Exit(code=1)
    try:
        import mkdocs  # noqa: F401, PLC0415
    except ImportError:
        typer.echo("mkdocs is not installed. Run: uv sync --group docs")
        raise typer.Exit(code=1) from None
    run_streamed(
        [sys.executable, "-m", "mkdocs", "serve", "-a", f"{host}:{port}"],
        cwd=project_root,
    )
