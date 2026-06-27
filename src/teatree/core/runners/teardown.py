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

    Two distinct outcomes (#877). A *refusal* (``cleanup_worktree`` raises
    ``RuntimeError`` — the #706 data-loss / hygiene guard) means the
    worktree was deliberately NOT torn down: the runner reports
    ``ok=False`` and the FSM stays put. A *completed teardown with step
    errors* (``CleanupResult.errors`` non-empty — a DB drop, pass-entry
    removal, recovery capture, or branch delete failed) means the worktree
    row IS gone but some side resource may linger; these were previously
    swallowed into a label string the caller never inspected (#932) and
    are now logged loudly and folded into the result detail so they reach
    the operator, without re-blocking a teardown the operator explicitly
    forced (the #706/#710 force-escape contract).

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
        refusals: list[str] = []
        step_errors: list[str] = []
        for worktree in worktrees:
            try:
                # FSM-driven teardown of a MERGED ticket: the FSM decided to tear
                # this worktree down, so it bypasses the opportunistic liveness
                # guard (respect_liveness=False) — a still-open session row on a
                # merged ticket must not block teardown. The #706 unpushed-commit
                # guard (force defaults False here) still protects real work.
                cleanup_result = cleanup_worktree(
                    worktree, force=self.force, strict_hygiene=False, respect_liveness=False
                )
            except RuntimeError as exc:
                logger.exception("teardown refused for %s", worktree.repo_path)
                refusals.append(f"{worktree.repo_path} ({worktree.branch}): {exc}")
                continue
            labels.append(cleanup_result.label)
            for err in cleanup_result.errors:
                logger.error("teardown step failed for %s: %s", worktree.repo_path, err)
                step_errors.append(f"{worktree.repo_path} ({worktree.branch}): {err}")

        if refusals:
            return RunnerResult(ok=False, detail="; ".join(refusals + step_errors))
        detail = f"tore down {len(labels)} worktree(s)"
        if step_errors:
            detail += f" [with errors: {'; '.join(step_errors)}]"
        return RunnerResult(ok=True, detail=detail)
