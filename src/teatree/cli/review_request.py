"""``t3 review-request`` — batch review request commands."""

from pathlib import Path

import typer

from teatree.cli.overlay import managepy

review_request_app = typer.Typer(no_args_is_help=True, help="Batch review requests.")


def _active_project() -> tuple[Path, str]:
    from teatree.cli import _find_project_root  # noqa: PLC0415
    from teatree.config import discover_active_overlay  # noqa: PLC0415

    active = discover_active_overlay()
    project = active.project_path if active and active.project_path else _find_project_root()
    return project, (active.name if active else "")


@review_request_app.command()
def discover() -> None:
    """Discover open merge requests awaiting review."""
    project, overlay_name = _active_project()
    managepy(project, "followup", "discover-mrs", overlay_name=overlay_name)


@review_request_app.command()
def check(mr_url: str = typer.Option(..., "--mr-url", help="Canonical MR/PR URL to dedup.")) -> None:
    """Race-safe pre-post dedup gate against LIVE Slack messages (#1084).

    Run this in the SAME turn as a review-request post and abort on
    ``"action": "suppress"`` — it reads the live review channel with the
    post-token and takes the atomic DB claim, so a duplicate (agent
    re-post or a user's manual out-of-band post) is impossible.
    """
    project, overlay_name = _active_project()
    managepy(project, "review_request_check", "--mr-url", mr_url, overlay_name=overlay_name)
