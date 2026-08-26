"""A PR owed since push must not be retried in silence forever (#792 follow-up).

``ensure-pr`` defers the create inside the pre-push hook and owes a
:class:`~teatree.core.models.pending_pull_request.PendingPullRequest`; the
dispatch tick drains it. An obligation the drain cannot discharge is the state
that stranded 68 branches with no PR at all, so it FAILs loud here rather than
looping quietly — a silent retry is indistinguishable from a working drain.

#3977: loud is not enough when the remedy is the wrong move. A branch a real
merge would conflict with produces a would-be PR that fights the base rather
than adding to it, so the remedy checks that first and asks for a rebase
decision from a person instead of printing "open a PR" for the hundredth time.
"""

from typing import TYPE_CHECKING

import typer

from teatree.core.worktree.branch_landed import RevertRisk, assess_revert_risk

if TYPE_CHECKING:
    from teatree.core.models.pending_pull_request import PendingPullRequest


def _revert_risk(repo_path: str, branch: str) -> RevertRisk:
    """Measure whether a merge from ``branch`` would conflict, never raising into the doctor run."""
    from teatree.core.worktree.branch_classification import (  # noqa: PLC0415 — deferred: keeps this leaf import-light
        effective_default_target,
    )

    try:
        return assess_revert_risk(repo_path, branch, effective_default_target(repo_path))
    except Exception:  # noqa: BLE001 — doctor check must never crash the run
        return RevertRisk()


def _remedy(row: "PendingPullRequest", risk: RevertRisk) -> str:
    """The action an undrainable obligation actually needs, decided from what its PR would do."""
    if risk.at_risk:
        paths = ", ".join(risk.conflicted_paths)
        return (
            f"A pull request from this branch would REVERT the base: a real merge conflicts at "
            f"{paths} — the branch predates a base refactor, so it needs a rebase decision from a "
            f"person, not a pull request. Either rebase {row.branch} onto the current base so the "
            f"next drain can open a PR that adds something, or drop the obligation with "
            f"t3 <overlay> pr discharge-pending {row.pk}."
        )
    from teatree.core.models.pending_pull_request import (  # noqa: PLC0415 — deferred: ORM import needs the app registry
        MAX_DRAIN_ATTEMPTS,
    )

    return (
        f"The branch is shipping with no PR. Fix: t3 <overlay> pr ensure-pr --repo {row.repo_path} "
        f"--branch {row.branch} (the drain keeps trying; past {MAX_DRAIN_ATTEMPTS} attempts it stops "
        f"being silent). If the branch genuinely needs no PR: t3 <overlay> pr discharge-pending {row.pk}."
    )


def check_pending_pull_requests() -> bool:
    """FAIL on every PR obligation still owed after ``MAX_DRAIN_ATTEMPTS`` drains."""
    from teatree.core.models.pending_pull_request import (  # noqa: PLC0415 — deferred: ORM import needs the app registry
        PendingPullRequest,
    )

    try:
        overdue = list(PendingPullRequest.objects.overdue())
    except Exception as exc:  # noqa: BLE001 — doctor check must never crash the run
        typer.echo(
            f"WARN  Pending-PR obligations UNVERIFIED: the obligation ledger could not be read "
            f"({exc.__class__.__name__}: {exc})."
        )
        return True
    for row in overdue:
        intended = f" (intended title: {row.intended_title!r})" if row.intended_title else ""
        typer.echo(
            f"FAIL  {row.branch} in {row.repo_path} has owed a pull request since {row.deferred_at:%Y-%m-%d %H:%M} "
            f"and {row.drain_attempts} drains could not open it{intended} — last: {row.last_error or row.reason}. "
            f"{_remedy(row, _revert_risk(row.repo_path, row.branch))}"
        )
    return not overdue


__all__ = ["check_pending_pull_requests"]
