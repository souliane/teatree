import logging

from teatree.config import load_config
from teatree.core.models import Ticket
from teatree.core.runners.base import RunnerBase, RunnerResult
from teatree.core.worktree_done import reap_done_worktree

logger = logging.getLogger(__name__)


class WorktreeTeardown(RunnerBase):
    """Tear down a done ticket's worktrees through the analyze-then-wipe reaper.

    The FSM-automatic teardown path (CORRECTION 3): ``execute_teardown`` enqueues
    this when the ticket reaches MERGED (the merge/ship transition). Each worktree
    funnels through :func:`reap_done_worktree` — the SAME consolidated
    done+redundant reaper ``clean-all`` and ``clean-merged`` use — so the loop
    tears a ticket's worktrees down the moment it is done, with the per-change
    analyze-before-wipe as the primary data-loss safety.

    Two dispositions. A *wipe* removes the git worktree + branch, the per-worktree
    DB, and the docker stack (containers/images/volumes); a per-worktree step error
    (DB drop, branch delete) is logged and folded into the detail without
    re-blocking (#932). Any *non-wiped* outcome — ``kept`` (a change NOT proven
    redundant: genuinely-unsynced work the FSM read as MERGED while an async ship
    never drained (#707/#708), or an uncommitted change (CORRECTION 1)), or a
    ``skipped``/``excluded``/``active`` worktree the reaper left standing — is a
    worktree that survived the teardown and MUST surface: it is logged and reported
    as a refusal (``ok=False``) so the FSM stays put and the operator sees it,
    never silently read as "tore down 0 worktree(s)" success. There is no recovery
    snapshot; potentially-needed work is KEPT, never force-destroyed.

    The teardown funnels through :func:`reap_done_worktree` with ``fsm_terminal=True``
    so the LIVENESS guard does not false-keep a just-merged worktree on the merge's
    own phase session / merge commit — the data-loss safety stays the analyze step.
    """

    def __init__(self, ticket: Ticket) -> None:
        self.ticket = ticket

    def run(self) -> RunnerResult:
        worktrees = list(self.ticket.worktrees.all())  # ty: ignore[unresolved-attribute]
        if not worktrees:
            return RunnerResult(ok=True, detail="no worktrees to tear down")

        workspace = load_config().user.workspace_dir
        wiped: list[str] = []
        stranded: list[str] = []
        step_errors: list[str] = []
        for worktree in worktrees:
            outcome = reap_done_worktree(worktree, workspace=workspace, dry_run=False, fsm_terminal=True)
            if outcome.action == "wiped":
                wiped.append(outcome.label)
                for err in outcome.errors:
                    logger.error("teardown step failed for %s: %s", worktree.repo_path, err)
                    step_errors.append(f"{worktree.repo_path} ({worktree.branch}): {err}")
                continue
            # Any non-wiped outcome left a worktree standing — surface it, never
            # let it read as success.
            logger.warning("teardown did not wipe %s (%s): %s", worktree.repo_path, outcome.action, outcome.label)
            stranded.append(f"{worktree.repo_path} ({worktree.branch}): {outcome.label}")

        if stranded:
            return RunnerResult(ok=False, detail="; ".join(stranded + step_errors))
        detail = f"tore down {len(wiped)} worktree(s)"
        if step_errors:
            detail += f" [with errors: {'; '.join(step_errors)}]"
        return RunnerResult(ok=True, detail=detail)
