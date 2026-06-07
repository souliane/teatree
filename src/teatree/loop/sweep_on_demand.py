"""On-demand pr_sweep trigger for a freshly recorded merge_safe verdict (#2026).

The periodic :class:`~teatree.loop.scanners.pr_sweep.PrSweepScanner` merges an
own PR only on its next tick after a ``merge_safe``
:class:`~teatree.core.models.review_verdict.ReviewVerdict` lands. With a 12-min
cadence, a verdict recorded just after a sweep tick waits a whole cadence —
long enough that a parallel human keystone-merges the PR first, so the
autonomous merge "never happens". This module closes the loop event-driven:
``record`` calls :func:`trigger_sweep_for_verdict` right after writing a
``merge_safe`` verdict, which rebuilds the verdict's own overlay sweep scanner
(same builder the tick uses) and runs the scanner's single-PR evaluation.

Best-effort: every failure degrades to a logged no-op so a sweep hiccup never
turns verdict recording into a command failure. The periodic sweep remains the
backstop — this only removes the cadence-length race window.
"""

import logging

from teatree.loop.scanners.pr_sweep import MergeAttempt, PrSweepScanner

logger = logging.getLogger(__name__)


def trigger_sweep_for_verdict(*, slug: str, pr_id: int, overlay: str) -> MergeAttempt | None:
    """Run the pr_sweep merge decision for *(slug, pr_id)* on *overlay*, now.

    Builds the same :class:`PrSweepScanner` the loop tick builds for *overlay*
    (so the gh token, ``solo_overlay`` posture, and notifier all match) and runs
    its single-PR :meth:`~teatree.loop.scanners.pr_sweep.PrSweepScanner.evaluate_one`.
    Returns the :class:`MergeAttempt` (``None`` when no sweep scanner exists for
    the overlay, the PR is no longer open, or any error degrades the trigger).
    """
    try:
        scanner = _sweep_scanner_for_overlay(overlay)
        if scanner is None:
            return None
        return scanner.evaluate_one(slug=slug, pr_id=pr_id)
    except Exception:
        logger.exception("on-demand pr_sweep trigger failed for %s#%d (overlay=%s)", slug, pr_id, overlay)
        return None


def _sweep_scanner_for_overlay(overlay: str) -> PrSweepScanner | None:
    from teatree.core.backend_factory import iter_overlay_backends  # noqa: PLC0415
    from teatree.loop.scanner_factories import _pr_sweep_scanner_for  # noqa: PLC0415
    from teatree.loop.scanner_factory_config import _user_slack_id_for_overlay  # noqa: PLC0415

    backend = next((candidate for candidate in iter_overlay_backends() if candidate.name == overlay), None)
    if backend is None:
        return None
    return _pr_sweep_scanner_for(backend, slack_user_id=_user_slack_id_for_overlay(overlay))
