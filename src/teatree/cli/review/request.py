"""``t3 review-request`` — batch review request commands."""

from pathlib import Path

import typer

from teatree.cli.overlay import managepy_core
from teatree.utils.django_bootstrap import ensure_django

review_request_app = typer.Typer(no_args_is_help=True, help="Batch review requests.")


def _active_project() -> tuple[Path, str]:
    """Resolve the (project_path, overlay_name) for review-request dispatch.

    Routes through :func:`config._active_overlay_entry` so the precedence
    matches ``get_overlay()``: ``T3_OVERLAY_NAME`` env first, then the
    cwd-``manage.py`` developer fallback, then the single configured
    overlay. The previous ``discover_active_overlay()``-only path resolved
    purely from the cwd ``manage.py`` dir (→ the teatree project when run
    from the teatree clone), so a review-request post for a *different*
    configured overlay could not resolve that overlay's Connect
    channel/token (#1103). The cwd-``manage.py`` discovery is preserved as
    the final fallback (dev-mode unbroken).

    The returned ``project_path`` is no longer used to pick the dispatch
    target (commands here are teatree-CORE — see :func:`managepy_core` and
    #1312); it is kept on the tuple only to preserve the existing return
    shape for callers and tests.
    """
    from teatree.cli import _find_project_root  # noqa: PLC0415
    from teatree.config import _active_overlay_entry  # noqa: PLC0415

    active = _active_overlay_entry()
    project = active.project_path if active and active.project_path else _find_project_root()
    return project, (active.name if active else "")


def _overlay_name_for_mr(mr_url: str) -> str:
    """Resolve the overlay that owns *mr_url* for a review-request dispatch.

    Prefers the cwd/env resolution of :func:`_active_project` (``T3_OVERLAY_NAME``
    env, then cwd-``manage.py`` dev fallback, then the single configured
    overlay). When that yields no overlay — the common case when ``t3`` is
    run from a clone whose directory name is not an overlay name (e.g. the
    teatree clone) on a multi-overlay install — fall back to inferring the
    owner from the MR URL via :func:`infer_overlay_for_url`.

    Without this fallback the dispatch ran with an empty ``T3_OVERLAY_NAME``,
    so the command subprocess hit ``get_overlay()``'s multi-overlay
    ambiguity, ``resolve_guard_target()`` returned ``None``, and every
    cross-overlay review request suppressed with
    ``no_review_channel_or_token`` regardless of cwd (#1471).

    Inference instantiates the registered overlays, so it needs the Django
    app registry. The ``review-request`` Typer group is otherwise
    Django-free (it only dispatches to a ``python -m teatree`` subprocess),
    so ``django.setup()`` is run here — idempotent — before inferring,
    matching the other DB-touching CLI wrappers.
    """
    _, overlay_name = _active_project()
    if overlay_name:
        return overlay_name

    from teatree.core.overlay_loader import infer_overlay_for_url  # noqa: PLC0415

    ensure_django()
    return infer_overlay_for_url(mr_url)


@review_request_app.command()
def discover() -> None:
    """Discover open merge requests awaiting review."""
    _, overlay_name = _active_project()
    managepy_core("followup", "discover-mrs", overlay_name=overlay_name)


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
    overlay_name = _overlay_name_for_mr(mr_url)
    managepy_core("review_request_check", "--mr-url", mr_url, overlay_name=overlay_name)


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
    overlay_name = _overlay_name_for_mr(mr_url)
    extra = ("--title", title) if title else ()
    managepy_core(
        "review_request_post",
        "--mr-url",
        mr_url,
        "--approver",
        approver,
        *extra,
        overlay_name=overlay_name,
    )
