"""The ``workspace clean-all`` orchestration body.

Its own module so :mod:`teatree.core.management.commands.workspace` stays under
the module-health LOC cap — the CLI method is a thin wrapper that delegates the
ordered cleanup passes here. The individual passes live in their focused sibling
modules (``cleanup`` / ``docker`` /
``isolated_roots`` / ``orphan_worktrees`` /
``stash``) and in :mod:`teatree.core.worktree.worktree_done` (the one
consolidated done+redundant Worktree-row reaper); this module only sequences them.
"""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from teatree.core.management.commands._workspace import helpers as _wh
from teatree.core.management.commands._workspace.cleanup import (
    WorktreeReaper,
    _raise_on_cleanup_failures,
    drop_orphan_databases,
    prune_branches,
)
from teatree.core.management.commands._workspace.docker import reap_orphan_worktree_docker
from teatree.core.management.commands._workspace.isolated_roots import reap_orphan_isolated_worktree_roots
from teatree.core.management.commands._workspace.orphan_worktrees import reap_orphan_raw_worktrees
from teatree.core.management.commands._workspace.stash import drop_orphaned_stashes
from teatree.core.worktree.worktree_done import reap_done_worktrees


@dataclass(frozen=True, slots=True)
class CleanAllIO:
    """The command's stdout/stderr write sinks, passed as one unit."""

    write_out: Callable[[str], object]
    write_err: Callable[[str], object]


def run_clean_all(
    workspace: Path,
    io: CleanAllIO,
    *,
    keep_dslr: int,
    dry_run: bool,
) -> list[str]:
    """Run the ordered clean-all passes against *workspace*, returning the result lines.

    The done-worktree reaper (:func:`reap_done_worktrees`) runs first — the one
    consolidated pass that wipes every done+redundant worktree (git worktree +
    branch, the per-worktree DB, docker containers/images/volumes) and keeps,
    with a reported reason, anything not-done or potentially-needed. It is fully
    unattended (CORRECTION 3): never prompts, never reads stdin.

    ``dry_run`` previews ONLY that reaper — each line names whether the worktree
    WOULD WIPE (with its done-signal source) or be KEPT — and removes nothing: the
    secondary destructive passes (empty-dir prune, orphan DBs/docker/env-roots,
    orphaned RAW worktrees, branch + stash prune, DSLR prune) are skipped entirely.
    A live run sequences all of them. A failed teardown line exits 1 via
    :func:`_raise_on_cleanup_failures` (the #932 failure contract).
    """
    in_use = _wh.dslr_tenants_in_use()  # before the reaper removes CREATED worktrees (#1306)

    cleaned: list[str] = reap_done_worktrees(workspace, dry_run=dry_run)
    if dry_run:
        return cleaned

    reaper = WorktreeReaper(workspace)
    cleaned.extend(reaper.remove_empty_ticket_dirs())
    cleaned.extend(drop_orphan_databases())
    cleaned.extend(reap_orphan_worktree_docker())
    cleaned.extend(reap_orphan_isolated_worktree_roots())
    cleaned.extend(reap_orphan_raw_worktrees(workspace))

    repo_root = Path.cwd()
    if (repo_root / ".git").exists():
        cleaned.extend(prune_branches(str(repo_root)))
        cleaned.extend(drop_orphaned_stashes(str(repo_root)))

    cleaned.extend(_wh.prune_dslr_snapshots_skipping(keep=keep_dslr, in_use_tenants=in_use))

    _raise_on_cleanup_failures(cleaned, io.write_out, io.write_err)
    return cleaned
