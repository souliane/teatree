import logging

from teatree.core.cleanup.cleanup import cleanup_worktree
from teatree.core.models import Worktree
from teatree.core.models.types import WorktreeExtra
from teatree.core.runners.base import RunnerBase, RunnerResult

logger = logging.getLogger(__name__)


class WorktreeTeardownRunner(RunnerBase):
    """Tear down a single worktree (docker down + DB drop + git worktree remove).

    Runs after ``Worktree.teardown()`` flips the FSM back to CREATED. Folds
    what was previously split between ``t3 <overlay> worktree teardown``
    (docker only) and ``t3 <overlay> worktree teardown`` (docker + DB +
    worktree row) into one canonical path so callers no longer have to
    chain commands. The runner owns docker-down + DB drop + git worktree
    removal; it then deletes the Worktree row.

    The transition body resets ``db_name`` and ``extra`` on the row to
    satisfy the FSM CREATED contract, so the runner accepts a snapshot of
    those fields captured before the reset — restored on the row before the
    cleanup helpers read them.

    ``force`` defaults to ``False`` so ``cleanup_worktree``'s unsynced-commit
    guard fires: this runner backs the ``execute_worktree_teardown`` task and
    the ``worktree``/``workspace teardown`` CLIs, and the FSM can reach a
    teardown-eligible state while the branch was never pushed (#706/#707/#708).
    Pass ``force=True`` only from an explicit operator override.
    """

    def __init__(
        self,
        worktree: Worktree,
        *,
        force: bool = False,
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
        # `cleanup_worktree` now owns `docker compose down` so every caller
        # (runner, sync backend, clean-all, clean-merged) tears down docker
        # the same way (#1306).
        try:
            # FSM/operator-driven teardown of this specific worktree bypasses the
            # opportunistic liveness guard (respect_liveness=False): the caller has
            # decided to tear it down. The #706 unpushed-commit guard (force
            # defaults False) still fires to protect real work.
            cleanup_result = cleanup_worktree(worktree, force=self.force, strict_hygiene=False, respect_liveness=False)
        except RuntimeError as exc:
            logger.warning("teardown refused for %s: %s", worktree.repo_path, exc)
            return RunnerResult(ok=False, detail=str(exc))

        # The worktree row IS gone (cleanup completed); a non-empty
        # ``errors`` list means a side resource (DB, pass entry, recovery
        # bundle, branch delete) failed. #877 — surface it loudly (logs +
        # ``str(cleanup_result)`` detail) instead of swallowing it into a
        # label the caller never reads (#932), but do not re-block a
        # teardown the operator explicitly forced (#706/#710 force-escape).
        for err in cleanup_result.errors:
            logger.error("teardown step failed for %s: %s", worktree.repo_path, err)

        return RunnerResult(ok=True, detail=str(cleanup_result))
