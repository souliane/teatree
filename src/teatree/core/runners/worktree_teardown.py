import logging

from teatree.core.cleanup import cleanup_worktree
from teatree.core.models import Worktree
from teatree.core.models.types import WorktreeExtra
from teatree.core.runners.base import RunnerBase, RunnerResult
from teatree.core.runners.worktree_start import compose_project, docker_compose_down

logger = logging.getLogger(__name__)


class WorktreeTeardownRunner(RunnerBase):
    """Tear down a single worktree (docker down + DB drop + git worktree remove).

    Runs after ``Worktree.teardown()`` flips the FSM back to CREATED. Folds
    what was previously split between ``t3 lifecycle teardown`` (docker only)
    and ``t3 lifecycle clean`` (docker + DB + worktree row) into one
    canonical path so callers no longer have to chain commands. The runner
    owns docker-down + DB drop + git worktree removal; it then deletes the
    Worktree row and releases the ticket's Redis slot when it was the last
    sibling.

    The transition body resets ``db_name`` and ``extra`` on the row to
    satisfy the FSM CREATED contract, so the runner accepts a snapshot of
    those fields captured before the reset — restored on the row before the
    cleanup helpers read them.
    """

    def __init__(
        self,
        worktree: Worktree,
        *,
        force: bool = True,
        snapshot_db_name: str | None = None,
        snapshot_extra: WorktreeExtra | None = None,
    ) -> None:
        self.worktree = worktree
        self.force = force
        if snapshot_db_name is not None:
            worktree.db_name = snapshot_db_name
        if snapshot_extra is not None:
            worktree.extra = dict(snapshot_extra)

    def run(self) -> RunnerResult:
        worktree = self.worktree
        project = compose_project(worktree)

        docker_compose_down(project)

        try:
            label = cleanup_worktree(worktree, force=self.force)
        except RuntimeError as exc:
            logger.warning("teardown refused for %s: %s", worktree.repo_path, exc)
            return RunnerResult(ok=False, detail=str(exc))

        return RunnerResult(ok=True, detail=label)
