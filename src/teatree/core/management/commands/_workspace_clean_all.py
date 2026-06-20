"""The ``workspace clean-all`` orchestration body.

Its own module so :mod:`teatree.core.management.commands.workspace` stays under
the module-health LOC cap — the CLI method is a thin wrapper that delegates the
ordered cleanup passes here. The individual passes live in their focused sibling
modules (``_workspace_cleanup`` / ``_workspace_reap`` / ``_workspace_docker`` /
``_workspace_isolated_roots`` / ``_workspace_orphan_worktrees`` /
``_workspace_stash``); this module only sequences them.
"""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from teatree.core.management.commands import _workspace_helpers as _wh
from teatree.core.management.commands._workspace_cleanup import (
    WorktreeReaper,
    _die,
    _raise_on_cleanup_failures,
    drop_orphan_databases,
    is_clean_ignored,
    prune_branches,
)
from teatree.core.management.commands._workspace_docker import reap_orphan_worktree_docker
from teatree.core.management.commands._workspace_isolated_roots import reap_orphan_isolated_worktree_roots
from teatree.core.management.commands._workspace_orphan_worktrees import reap_orphan_raw_worktrees
from teatree.core.management.commands._workspace_reap import _is_interactive, reap_one_worktree
from teatree.core.management.commands._workspace_stash import drop_orphaned_stashes
from teatree.core.models import Worktree

if TYPE_CHECKING:
    from teatree.core.management.commands._workspace_orphan_worktrees import ReapUnsyncedPolicy

_VALID_REAP_UNSYNCED = {"keep", "snapshot"}


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
    reap_unsynced: str,
    interactive: bool,
) -> list[str]:
    """Run the ordered clean-all passes against *workspace*, returning the result lines.

    Validates ``reap_unsynced`` up front (exit 1 on a bad value), then sequences:
    squash-merged Worktree rows, CREATED-row reap, empty-ticket-dir prune, orphan
    DBs, docker/env-roots, orphaned RAW worktrees
    (#2361), branch + stash prune in the cwd repo, and DSLR snapshot prune. A
    failed push/abandon line exits 1 via :func:`_raise_on_cleanup_failures` (the
    #932 failure contract).
    """
    if reap_unsynced not in _VALID_REAP_UNSYNCED:
        _die(io.write_err, f"--reap-unsynced must be 'keep' or 'snapshot', got {reap_unsynced!r}")
    policy: ReapUnsyncedPolicy = "snapshot" if reap_unsynced == "snapshot" else "keep"
    interactive = interactive and _is_interactive()
    in_use = _wh.dslr_tenants_in_use()  # before the cleanup loop reaps CREATED worktrees (#1306)

    cleaned: list[str] = []
    reaper = WorktreeReaper(workspace)
    cleaned.extend(reaper.reap_squash_merged_worktrees(interactive=interactive))
    for wt in Worktree.objects.filter(state=Worktree.State.CREATED):
        if is_clean_ignored(wt.branch, overlay=wt.overlay):
            cleaned.append(f"SKIPPED '{wt.branch}': matches clean_ignore — keeping")
            continue
        cleaned.append(reap_one_worktree(wt, interactive=interactive))

    cleaned.extend(reaper.remove_empty_ticket_dirs())
    cleaned.extend(drop_orphan_databases())
    cleaned.extend(reap_orphan_worktree_docker())
    cleaned.extend(reap_orphan_isolated_worktree_roots())
    cleaned.extend(reap_orphan_raw_worktrees(workspace, reap_unsynced=policy))

    repo_root = Path.cwd()
    if (repo_root / ".git").exists():
        cleaned.extend(prune_branches(str(repo_root)))
        cleaned.extend(drop_orphaned_stashes(str(repo_root)))

    cleaned.extend(_wh.prune_dslr_snapshots_skipping(keep=keep_dslr, in_use_tenants=in_use))

    _raise_on_cleanup_failures(cleaned, io.write_out, io.write_err)
    return cleaned
