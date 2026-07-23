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
from teatree.core.management.commands._workspace.broken_worktrees import reap_broken_worktree_dirs
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
from teatree.core.worktree.worktree_roots import scanned_worktree_roots


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

    ``dry_run`` previews EVERY pass and removes nothing (souliane/teatree#3489).
    Each pass computes its candidate set exactly as a live run would and skips
    only the mutation, so a would-be line and its live counterpart cannot
    disagree about scope: the done-worktree reaper names WIPE/KEPT with its
    done-signal source, and each secondary pass renders its candidates through
    :func:`~teatree.core.management.commands._workspace.preview.preview_line`. A
    preview that under-reports what a destructive command will do is worse than
    no preview at all. A failed teardown line exits 1 via
    :func:`_raise_on_cleanup_failures` (the #932 failure contract).

    One pass is preview-blind: :func:`reap_broken_worktree_dirs` takes no
    ``dry_run``, so a dry run reports it as NOT PREVIEWED rather than silently
    omitting it.
    """
    in_use = _wh.dslr_tenants_in_use()  # before the reaper removes CREATED worktrees (#1306)

    cleaned: list[str] = reap_done_worktrees(workspace, dry_run=dry_run)

    reaper = WorktreeReaper(workspace)
    cleaned.extend(reaper.remove_empty_ticket_dirs(dry_run=dry_run))
    cleaned.extend(drop_orphan_databases(dry_run=dry_run))
    cleaned.extend(reap_orphan_worktree_docker(dry_run=dry_run))
    cleaned.extend(reap_orphan_isolated_worktree_roots(dry_run=dry_run))
    cleaned.extend(reap_orphan_raw_worktrees(workspace, dry_run=dry_run))
    # Runs AFTER the raw-orphan pass: that one disposes of checkouts git can
    # still resolve, leaving only the dirs git cannot resolve at all (#3583).
    if dry_run:
        cleaned.append("NOT PREVIEWED: broken-worktree-dir reap (pass has no dry-run mode) — a live run includes it")
    else:
        cleaned.extend(reap_broken_worktree_dirs(*scanned_worktree_roots(workspace)))

    repo_root = Path.cwd()
    if (repo_root / ".git").exists():
        cleaned.extend(prune_branches(str(repo_root), dry_run=dry_run))
        cleaned.extend(drop_orphaned_stashes(str(repo_root), dry_run=dry_run))
    else:
        # Both passes are cwd-gated, so from a non-repo cwd a LIVE run is a silent
        # no-op too. A preview that hid that would misreport the command's scope.
        cleaned.append(f"SKIPPED branch + stash prune: cwd {repo_root} is not a git repo")

    cleaned.extend(_wh.prune_dslr_snapshots_skipping(keep=keep_dslr, in_use_tenants=in_use, dry_run=dry_run))

    if dry_run:
        return cleaned
    _raise_on_cleanup_failures(cleaned, io.write_out, io.write_err)
    return cleaned
