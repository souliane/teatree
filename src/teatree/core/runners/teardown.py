import logging

from teatree.core.cleanup import cleanup_worktree
from teatree.core.runners.base import RunnerBase, RunnerResult

logger = logging.getLogger(__name__)


class WorktreeTeardown(RunnerBase):
    """Tear down every worktree owned by a MERGED ticket.

    Iterates ``ticket.worktrees`` and delegates to ``cleanup_worktree`` for
    each one (git worktree removal, branch deletion, DB drop, overlay
    cleanup hooks). Errors on a single worktree are captured but do not
    abort the rest — the runner reports a combined success/failure label.
    """

    def run(self) -> RunnerResult:
        ticket = self.ticket
        worktrees = list(ticket.worktrees.all())  # ty: ignore[unresolved-attribute]
        if not worktrees:
            return RunnerResult(ok=True, detail="no worktrees to tear down")

        labels: list[str] = []
        errors: list[str] = []
        for worktree in worktrees:
            try:
                labels.append(cleanup_worktree(worktree, force=True))
            except RuntimeError as exc:
                logger.exception("teardown failed for %s", worktree.repo_path)
                errors.append(f"{worktree.repo_path} ({worktree.branch}): {exc}")

        if errors:
            return RunnerResult(ok=False, detail="; ".join(errors))
        return RunnerResult(ok=True, detail=f"tore down {len(labels)} worktree(s)")
