"""Rebuild a coding envelope's ``files_modified`` from the commit it already landed.

The #3263 recovery, split out of :mod:`teatree.agents.attempt_recorder` as its own
concern: this module reads git and the ticket's worktrees, where the recorder reads
result envelopes.
"""

from teatree.agents.landing_verification import commits_ahead_or_unknown, landing_verification_error
from teatree.agents.result_schema import AgentResultBlob
from teatree.core.modelkit.phases import normalize_phase
from teatree.core.models import Task, Worktree
from teatree.utils import git
from teatree.utils.run import CommandFailedError

_SALVAGEABLE_PHASES = frozenset({"coding", "debugging"})


def salvage_coding_result(task: Task, result: AgentResultBlob, *, phase: str) -> AgentResultBlob | None:
    """Return *result* with ``files_modified`` synthesized from the landed commit, or ``None``.

    The #3263 recovery: a coder committed real work but omitted the trailing
    ``files_modified`` envelope, so the evidence gate refuses and the branch is
    stranded. When the ticket worktree has a NEW commit ahead of its base AND is
    clean (``landing_verification_error`` passes — so this never salvages dirty or
    commit-less work), the committed diff's file paths ARE the evidence: synthesize
    ``files_modified`` from them so the task COMPLETES on the real landed work.
    ``None`` for a non-coding phase, or when there is nothing clean to salvage —
    the caller then records the honest evidence refusal.
    """
    if normalize_phase(phase or task.phase) not in _SALVAGEABLE_PHASES:
        return None
    if landing_verification_error(task, phase=phase):
        return None
    files = _committed_file_changes(task)
    if not files:
        return None
    salvaged = dict(result)
    salvaged["files_modified"] = files
    return salvaged


def _committed_file_changes(task: Task) -> list[dict[str, str]]:
    """``files_modified`` entries for the first ticket worktree with a commit ahead, else ``[]``.

    A worktree whose probe cannot answer is one nothing can be salvaged FROM, so
    it is skipped like a commit-less one — never allowed to abort the scan before
    it reaches the sibling that did land the work.
    """
    for worktree in Worktree.objects.for_ticket(task.ticket):
        if commits_ahead_or_unknown(worktree) is not True:
            continue
        paths = _committed_paths(worktree)
        if paths:
            return [{"path": path, "action": "modified"} for path in paths]
    return []


def _committed_paths(worktree: Worktree) -> list[str]:
    # Reached only after ``commits_ahead_or_unknown`` proved a valid path + branch.
    repo_path = (worktree.extra or {}).get("worktree_path") or worktree.repo_path
    base = _base_ref(repo_path)
    try:
        out = git.run(repo=repo_path, args=["diff", "--name-only", f"{base}..{worktree.branch}"])
    except (CommandFailedError, OSError):
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def _base_ref(repo_path: str) -> str:
    try:
        return f"origin/{git.default_branch(repo_path)}"
    except (CommandFailedError, RuntimeError):
        return "main"
