"""A PR owed since push must not be retried in silence forever (#792 follow-up).

``ensure-pr`` defers the create inside the pre-push hook and owes a
:class:`~teatree.core.models.pending_pull_request.PendingPullRequest`; the
dispatch tick drains it. An obligation the drain cannot discharge is the state
that stranded 68 branches with no PR at all, so it FAILs loud here rather than
looping quietly — a silent retry is indistinguishable from a working drain.
"""

import typer


def check_pending_pull_requests() -> bool:
    """FAIL on every PR obligation still owed after ``MAX_DRAIN_ATTEMPTS`` drains."""
    from teatree.core.models.pending_pull_request import (  # noqa: PLC0415 — deferred: ORM import needs the app registry
        MAX_DRAIN_ATTEMPTS,
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
            f"The branch is shipping with no PR. Fix: t3 <overlay> pr ensure-pr --repo {row.repo_path} "
            f"--branch {row.branch} (the drain keeps trying; past {MAX_DRAIN_ATTEMPTS} attempts it stops being "
            f"silent). If the branch genuinely needs no PR: t3 <overlay> pr discharge-pending {row.pk}."
        )
    return not overdue


__all__ = ["check_pending_pull_requests"]
