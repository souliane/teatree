"""Overlay-specific worktree-teardown steps, split out of :mod:`teatree.core.cleanup.cleanup`.

These run an overlay's own teardown hooks (custom cleanup steps, the
external-resource reaper). They operate purely through the overlay object's
methods, so they are no-ops for a worktree whose overlay is unregistered — the
overlay-free reap path keeps the git/DB teardown that lives in ``cleanup``.
"""

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from teatree.core.models import Worktree
    from teatree.core.overlay import OverlayBase

logger = logging.getLogger(__name__)


def run_overlay_cleanup_steps(overlay: "OverlayBase | None", worktree: "Worktree", step_errors: list[str]) -> None:
    """Run the overlay's custom cleanup-step hooks; no-op for an unregistered overlay."""
    if overlay is None:
        return
    for step in overlay.get_cleanup_steps(worktree):
        try:
            step.callable()
        except Exception as exc:
            logger.exception("cleanup step failed for %s: %s", worktree.repo_path, step.description)
            step_errors.append(f"{step.description}: {exc}")


def reap_external_resources(overlay: "OverlayBase", worktree: "Worktree", step_errors: list[str]) -> str:
    """Run the overlay's external-resource reaper, returning a label suffix.

    Appends a descriptive string to *step_errors* on failure (collect-and-surface,
    never crash mid-teardown) and returns the joined outcomes as a ``" — …"``
    suffix for the cleanup label, or ``""`` when nothing was removed or it failed.
    """
    try:
        reaped = overlay.reap_worktree_external_resources(worktree)
    except Exception as exc:
        logger.exception("external-resource reap failed for %s (%s)", worktree.repo_path, worktree.branch)
        step_errors.append(f"external-resource reap failed for {worktree.branch}: {exc}")
        return ""
    return " — " + "; ".join(reaped) if reaped else ""
