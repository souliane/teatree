"""Shared dual-forge PR payload extractors (#7 / SIG-2).

One canonical head-SHA reader for both forge shapes, imported by every scanner
that emits a PR signal — ``reviewer_prs`` (its review-cache keys) and ``my_prs``
(the ``my_pr.failed`` payload that feeds the ``RedMrFixAttempt`` ledger). A
single symbol at every extraction site kills the sibling-reimplementation drift
the audit flagged: a scanner that re-derives the head SHA locally would diverge
on the next forge-shape change, and the divergence would be silent.
"""

from typing import cast

from teatree.types import RawAPIDict


def head_sha(pr: RawAPIDict) -> str:
    """The PR's head commit SHA across forge shapes, or ``""`` when absent.

    GitLab MR list endpoints expose a top-level ``sha``; GitHub PRs nest it
    under ``head.sha``; GitLab MR detail endpoints carry it under
    ``diff_refs.head_sha``.
    """
    sha = pr.get("sha")
    if isinstance(sha, str):
        return sha
    head = pr.get("head")
    if isinstance(head, dict):
        nested = cast("RawAPIDict", head).get("sha")
        if isinstance(nested, str):
            return nested
    diff_refs = pr.get("diff_refs")
    if isinstance(diff_refs, dict):
        nested = cast("RawAPIDict", diff_refs).get("head_sha")
        if isinstance(nested, str):
            return nested
    return ""
