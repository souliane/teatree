"""Refuse a coding result for in-scope code whose revert sweep is absent, stale, or lists an unpinned hunk."""

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from teatree.core.modelkit.phases import normalize_phase
from teatree.core.models.ticket_worktree_checks import _resolve_base_branch
from teatree.core.models.worktree import Worktree
from teatree.quality.revert_sweep import RevertSweepError, in_scope_diff_paths, read_revert_sweep
from teatree.utils import git
from teatree.utils.run import CommandFailedError

if TYPE_CHECKING:
    from teatree.core.models.task import Task

logger = logging.getLogger(__name__)

SWEEP_UNPINNED_PREFIX = "sweep_unpinned:"

_SWEPT_PHASES = frozenset({"coding", "debugging"})


def sweep_report_error(task: "Task", *, phase: str = "") -> str:
    if normalize_phase(phase or task.phase) not in _SWEPT_PHASES:
        return ""
    for worktree in Worktree.objects.for_ticket(task.ticket):
        path = worktree.worktree_path
        if path and Path(path).is_dir() and (error := _checkout_error(Path(path))):
            return error
    return ""


def _checkout_error(repo: Path) -> str:
    try:
        head = git.head_sha(str(repo))
        base = git.merge_base(str(repo), _resolve_base_branch(str(repo)))
        scoped = in_scope_diff_paths(repo, base=base, head=head)
    except (CommandFailedError, OSError):
        logger.warning("could not read the git state of %s — the revert-sweep receipt was not checked", repo)
        return ""
    return _receipt_error(repo, head, scoped) if scoped else ""


def _receipt_error(repo: Path, head: str, scoped: tuple[str, ...]) -> str:
    command = f"t3 tool revert-sweep --expect-sha {head}"
    try:
        receipt = read_revert_sweep(repo)
    except RevertSweepError as exc:
        return f"{SWEEP_UNPINNED_PREFIX} {exc}; rerun `{command}`"
    if receipt is None:
        return (
            f"{SWEEP_UNPINNED_PREFIX} no revert-sweep receipt for {', '.join(scoped)}; "
            f"commit, run `{command}`, pin every survivor"
        )
    if receipt.head_sha != head:
        return (
            f"{SWEEP_UNPINNED_PREFIX} stale revert-sweep receipt for {receipt.head_sha[:12]}, "
            f"HEAD is {head[:12]}; rerun `{command}`"
        )
    unpinned = (*receipt.survivors, *receipt.unmeasured)
    if unpinned:
        hunks = ", ".join(f"{verdict.status} {verdict.location}" for verdict in unpinned)
        return f"{SWEEP_UNPINNED_PREFIX} no test pins {hunks}; pin each hunk with a test and rerun `{command}`"
    return ""
