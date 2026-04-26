"""``t3 review-request`` — batch review request commands."""

import typer

from teatree.cli.overlay import managepy

review_request_app = typer.Typer(no_args_is_help=True, help="Batch review requests.")


@review_request_app.command()
def discover() -> None:
    """Discover open merge requests awaiting review."""
    from teatree.cli import _find_project_root  # noqa: PLC0415
    from teatree.config import discover_active_overlay  # noqa: PLC0415

    active = discover_active_overlay()
    project = active.project_path if active and active.project_path else _find_project_root()
    overlay_name = active.name if active else ""
    managepy(project, "followup", "discover-mrs", overlay_name=overlay_name)
