"""Checkouts that hold review evidence, or are marked rescue or backup work, lose nothing to a space reclaim (#5615).

A reclaim that deletes from disk must never take what a reviewer is relying on. Two facts say a
checkout is that, and neither needs a ticket row: the review-evidence receipt a reviewer wrote
into its git admin dir (``t3 tool review-evidence``), and a branch named for evidence, rescue or
backup work. Both are read from the checkout itself, because the ``Worktree`` row can be absent
or stale.

State this module cannot read counts as evidence: a guard that authorises a deletion fails closed.
"""

import os
from pathlib import Path

from teatree.quality.review_evidence import ReviewEvidenceError, receipt_path
from teatree.utils.run import CommandFailedError, run_allowed_to_fail

_PROTECTED_BRANCH_MARKERS = ("evidence", "rescue", "backup")
_DETACHED_HEAD_EXIT = 1


def evidence_checkout_reason(checkout: Path, *, recorded_branch: str = "") -> str:
    """Why *checkout* must keep everything a reclaim could take, or ``""`` when nothing marks it."""
    try:
        if os.path.lexists(receipt_path(checkout)):
            return "it holds a review-evidence receipt"
        branches = (_checked_out_branch(checkout), recorded_branch)
    except (CommandFailedError, ReviewEvidenceError, OSError) as exc:
        return f"its evidence state could not be read ({exc}), so it is treated as evidence"
    for branch in branches:
        if any(marker in branch.lower() for marker in _PROTECTED_BRANCH_MARKERS):
            return f"its branch '{branch}' marks protected evidence, rescue or backup work"
    return ""


def _checked_out_branch(checkout: Path) -> str:
    result = run_allowed_to_fail(
        ["git", "-C", str(checkout), "symbolic-ref", "--short", "-q", "HEAD"],
        expected_codes=(0, _DETACHED_HEAD_EXIT),
    )
    return result.stdout.strip()


__all__ = ["evidence_checkout_reason"]
