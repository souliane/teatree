import logging

from teatree.core.cleanup import cleanup_worktree
from teatree.core.models import Ticket
from teatree.core.runners.base import RunnerBase, RunnerResult

logger = logging.getLogger(__name__)


class WorktreeTeardown(RunnerBase):
    """Tear down every worktree owned by a MERGED ticket.

    Iterates ``ticket.worktrees`` and delegates to ``cleanup_worktree`` for
    each one (git worktree removal, branch deletion, DB drop, overlay
    cleanup hooks). Errors on a single worktree are captured but do not
    abort the rest — the runner reports a combined success/failure label.

    This is the *automated* teardown path: ``execute_teardown`` enqueues it
    when the ticket FSM reaches MERGED. The FSM can read MERGED while the
    branch was never actually pushed (async ship never drained — #707/#708),
    so this path must NOT force-bypass ``cleanup_worktree``'s unsynced-commit
    guard. ``force`` defaults to ``False`` (the guard fires); pass
    ``force=True`` only from an explicit operator override (e.g.
    ``--force``). #706 — forcing here physically destroyed worktrees with
    unpushed work.
    """

    def __init__(self, ticket: Ticket, *, force: bool = False) -> None:
        self.ticket = ticket
        self.force = force

    def run(self) -> RunnerResult:
        ticket = self.ticket
        worktrees = list(ticket.worktrees.all())  # ty: ignore[unresolved-attribute]
        if not worktrees:
            return RunnerResult(ok=True, detail="no worktrees to tear down")

        labels: list[str] = []
        errors: list[str] = []
        for worktree in worktrees:
            try:
                labels.append(cleanup_worktree(worktree, force=self.force, strict_hygiene=False))
            except RuntimeError as exc:
                logger.exception("teardown failed for %s", worktree.repo_path)
                errors.append(f"{worktree.repo_path} ({worktree.branch}): {exc}")

        if errors:
            return RunnerResult(ok=False, detail="; ".join(errors))
        return RunnerResult(ok=True, detail=f"tore down {len(labels)} worktree(s)")
