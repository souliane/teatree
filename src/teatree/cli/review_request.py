"""``t3 review-request`` — batch review request commands."""

from pathlib import Path

import typer

from teatree.cli.overlay import managepy

review_request_app = typer.Typer(no_args_is_help=True, help="Batch review requests.")


def _active_project() -> tuple[Path, str]:
    """Resolve the (project_path, overlay_name) the managepy delegate runs as.

    Routes through :func:`config._active_overlay_entry` so the precedence
    matches ``get_overlay()``: ``T3_OVERLAY_NAME`` env first, then the
    cwd-``manage.py`` developer fallback, then the single configured
    overlay. The previous ``discover_active_overlay()``-only path resolved
    purely from the cwd ``manage.py`` dir (→ the teatree project when run
    from the teatree clone), so a review-request post for a *different*
    configured overlay could not resolve that overlay's Connect
    channel/token (#1103). The cwd-``manage.py`` discovery is preserved as
    the final fallback (dev-mode unbroken).
    """
    from teatree.cli import _find_project_root  # noqa: PLC0415
    from teatree.config import _active_overlay_entry  # noqa: PLC0415

    active = _active_overlay_entry()
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
    post-token to detect a duplicate (agent re-post or a user's manual
    out-of-band post). It is strictly decision-only: it takes NO durable
    ``ReviewRequestPost`` claim (``peek_should_post_review_request``), so
    it can never leave an orphan that wedges a later real post (#1103).
    """
    project, overlay_name = _active_project()
    managepy(project, "review_request_check", "--mr-url", mr_url, overlay_name=overlay_name)


@review_request_app.command()
def post(
    mr_url: str = typer.Option(..., "--mr-url", help="Canonical MR/PR URL to post."),
    approver: str = typer.Option(..., "--approver", help="User id that recorded the #960 approval."),
    title: str = typer.Option("", "--title", help="Review-request subject (recommended)."),
) -> None:
    """Sanctioned authorized review-request post: #1094 dedup + #960 approval + post (#1098).

    One classifier-legible transaction: the #1084 live-channel dedup, the
    #960 recorded-approval chokepoint (``t3 review approve-on-behalf`` is
    the only way to satisfy it), then the post. Refuses with the exact
    ``approve-on-behalf`` remediation when no recorded approval matches.
    """
    project, overlay_name = _active_project()
    extra = ("--title", title) if title else ()
    managepy(
        project,
        "review_request_post",
        "--mr-url",
        mr_url,
        "--approver",
        approver,
        *extra,
        overlay_name=overlay_name,
    )
