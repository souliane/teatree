"""The ``workspace release-dead-rows`` output shapes — machine payload and human lines.

The engine (:mod:`teatree.core.worktree.dead_row_release`) classifies and deletes;
this renders. Both surfaces are derived from the SAME
:class:`~teatree.core.worktree.dead_row_release.DeadRowReleaseOutcome`, so the JSON
a front-end parses and the prose an operator reads can never disagree about which
rows were released and which were kept.
"""

from typing import IO, TypedDict

from teatree.core.worktree.dead_row_release import DeadRowReleaseOutcome


class DeadRowReport(TypedDict):
    """One classified row, as the machine channel reports it."""

    worktree_pk: int
    branch: str
    path: str
    disposition: str
    reason: str
    released: bool


class DeadRowReleaseReport(TypedDict):
    """The whole pass: whether it deleted, how many, and every row it judged."""

    applied: bool
    released: int
    rows: list[DeadRowReport]


def build_dead_row_report(outcome: DeadRowReleaseOutcome) -> DeadRowReleaseReport:
    """The structured payload for *outcome*."""
    return {
        "applied": outcome.applied,
        "released": len(outcome.released_pks),
        "rows": [
            {
                "worktree_pk": verdict.worktree_pk,
                "branch": verdict.branch,
                "path": verdict.path,
                "disposition": str(verdict.disposition),
                "reason": verdict.reason,
                "released": verdict.worktree_pk in outcome.released_pks,
            }
            for verdict in outcome.verdicts
        ],
    }


def write_dead_row_lines(outcome: DeadRowReleaseOutcome, stream: IO[str]) -> None:
    """The human rendering of *outcome* — one line per classified row."""
    for line in outcome.render():
        stream.write(f"{line}\n")


__all__ = ["DeadRowReleaseReport", "DeadRowReport", "build_dead_row_report", "write_dead_row_lines"]
