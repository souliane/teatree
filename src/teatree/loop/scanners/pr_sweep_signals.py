"""``ScanSignal`` builders for :mod:`teatree.loop.scanners.pr_sweep`.

The scanner core (:class:`~teatree.loop.scanners.pr_sweep.PrSweepScanner`) lives in
``pr_sweep``; this module holds the pure signal-construction functions it calls from
``scan()``. Splitting them out keeps the scanner module focused on the decision ladder
and under the module-health LOC cap (same split rationale as ``pr_sweep_decision`` /
``pr_sweep_adapters``).
"""

from teatree.loop.scanners.base import ScanSignal
from teatree.loop.scanners.pr_sweep_types import MergeAttempt


def pass_signal(*, slug: str, pr_ids: list[int], overlay: str) -> ScanSignal:
    """One per-slug ``pr_sweep.pass`` signal naming every PR the listing found open.

    The aged-skip streak ledger (:mod:`teatree.loop.pr_sweep_skip_surface`) reads this
    as the ground truth for what is still open on *slug*: a streak row for a PR absent
    from *pr_ids* is a finished fact (merged or closed), not a stall, and is purged.
    """
    return ScanSignal(
        kind="pr_sweep.pass",
        summary=f"{slug} pass: {len(pr_ids)} open PR(s)",
        payload={"slug": slug, "pr_ids": pr_ids, "overlay": overlay},
    )


def signal_from_attempt(attempt: MergeAttempt, *, overlay: str) -> ScanSignal:
    return ScanSignal(
        kind="pr_sweep.merged" if attempt.merged else f"pr_sweep.{attempt.decision}",
        summary=f"{attempt.slug}#{attempt.pr_id} {attempt.decision} ({attempt.reason})",
        payload={
            "slug": attempt.slug,
            "pr_id": attempt.pr_id,
            "decision": attempt.decision,
            "reason": attempt.reason,
            "merged": attempt.merged,
            "merged_sha": attempt.merged_sha,
            "overlay": overlay,
            "url": attempt.url,
            "review_dispatched": attempt.review_dispatched,
            "failing_required": list(attempt.failing_required),
            "base_current": attempt.base_current,
            "held_verdicts": [list(ref) for ref in attempt.held_verdicts],
            "authorizing_verdict": None if attempt.authorizing_verdict is None else list(attempt.authorizing_verdict),
        },
    )
