"""Directive #8 — drive in-flight sub-agent worktrees through fast-push before termination.

When a session hands off (or the orchestrator shuts down), a sub-agent still
holding unpushed work would otherwise be killed with that work stranded on a
volatile worktree. This module is the coupling the directive asks for: the
orchestrator enumerates the clone's sub-agent worktrees that still carry pending
work and runs the leak-gated :class:`~teatree.core.fast_push.FastPusher` on each,
so the work is committed, pushed, and PR-upserted FIRST — "everybody follows the
command" before anyone is terminated.

Best-effort and idempotent: a clean (synced) worktree is skipped, the
orchestrator's own worktree is excluded, and a push failure on one worktree is
recorded and never blocks the others or the hand-off itself. The leak gates live
inside ``FastPusher``, so nothing here can bypass them.
"""

import logging
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from teatree.core.fast_push import FastPusher, FastPushOutcome
from teatree.utils.git import WorktreeRecord, default_branch, list_worktrees, log_oneline, run, status_porcelain

logger = logging.getLogger(__name__)

_SUBAGENT_WORKTREES_DIRNAME = "worktrees"
_SUBAGENT_PARENT_DIRNAME = ".claude"
_SUBAGENT_DIR_PREFIX = "agent-"


class _RunsFastPush(Protocol):
    def run(self) -> FastPushOutcome: ...  # pragma: no branch


PusherFactory = Callable[[Path], _RunsFastPush]


@dataclass(frozen=True, slots=True)
class SubagentPush:
    """The outcome of driving one sub-agent worktree through fast-push."""

    worktree: Path
    branch: str
    driven: bool
    outcome: FastPushOutcome | None = None
    error: str = ""


def _is_subagent_worktree(path: Path) -> bool:
    """True iff *path* is a spawned sub-agent worktree (``.claude/worktrees/agent-*``).

    The orchestrator's OWN checkout and any human/ticket worktree live elsewhere;
    only a direct ``agent-``-prefixed child of ``.claude/worktrees`` is a spawned
    sub-agent whose unpushed work the hand-off must rescue.
    """
    return (
        path.name.startswith(_SUBAGENT_DIR_PREFIX)
        and path.parent.name == _SUBAGENT_WORKTREES_DIRNAME
        and path.parent.parent.name == _SUBAGENT_PARENT_DIRNAME
    )


def _push_base(path: Path) -> str:
    """The ref a push would advance past: the upstream, else ``origin/<default>``.

    ``""`` when neither resolves (a repo with no default branch), which the
    caller reads as "no unpushed commits provable" → skip.
    """
    upstream = run(repo=str(path), args=["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"]).strip()
    if upstream:
        return upstream
    try:
        default = default_branch(str(path))
    except (RuntimeError, ValueError):
        return ""
    return f"origin/{default}" if default else ""


def _has_pending_work(path: Path) -> bool:
    """True iff *path* has uncommitted changes OR commits not yet on the remote.

    A dirty working tree is pending; so is a branch ahead of its push base
    (unpushed commits, incl. a fresh feature branch that was never pushed). A
    clean, fully-synced worktree has nothing to rescue and is skipped.
    """
    if status_porcelain(str(path)).strip():
        return True
    base = _push_base(path)
    if not base:
        return False
    return bool(log_oneline(str(path), f"{base}..HEAD").strip())


def in_flight_subagent_worktrees(repo: str = ".", *, exclude: Sequence[Path] = ()) -> Iterator[WorktreeRecord]:
    """Yield the sub-agent worktrees of *repo*'s clone that still carry pending work.

    Excludes any path in *exclude* (the orchestrator's own worktree) and every
    non-sub-agent or clean worktree. Read-only enumeration over the single
    ``git worktree list`` parse.
    """
    excluded = {p.resolve() for p in exclude}
    for record in list_worktrees(repo):
        path = record.path
        if path.resolve() in excluded:
            continue
        if not _is_subagent_worktree(path):
            continue
        if not _has_pending_work(path):
            continue
        yield record


def _default_pusher(worktree: Path) -> FastPusher:
    message = f"chore(handover): fast-push checkpoint before termination ({worktree.name})"
    return FastPusher(repo=worktree, message=message)


def drive_subagents_to_fast_push(
    repo: str = ".",
    *,
    exclude: Sequence[Path] = (),
    pusher_factory: PusherFactory | None = None,
) -> list[SubagentPush]:
    """Drive every in-flight sub-agent worktree of *repo* through leak-gated fast-push.

    The directive #8 coupling: called from the hand-off/shutdown seam so a
    sub-agent's work is committed + pushed + PR-upserted BEFORE it can be
    terminated. Each worktree is pushed independently and best-effort — a
    failure on one is recorded on its :class:`SubagentPush` and never aborts the
    rest. Returns one :class:`SubagentPush` per worktree acted on.
    """
    factory = pusher_factory or _default_pusher
    pushes: list[SubagentPush] = []
    for record in in_flight_subagent_worktrees(repo, exclude=exclude):
        try:
            outcome = factory(record.path).run()
        except Exception as exc:
            logger.exception("handover: fast-push failed for sub-agent worktree %s", record.path)
            pushes.append(SubagentPush(worktree=record.path, branch=record.branch, driven=False, error=str(exc)))
            continue
        pushes.append(SubagentPush(worktree=record.path, branch=record.branch, driven=True, outcome=outcome))
    return pushes


__all__ = [
    "PusherFactory",
    "SubagentPush",
    "drive_subagents_to_fast_push",
    "in_flight_subagent_worktrees",
]
