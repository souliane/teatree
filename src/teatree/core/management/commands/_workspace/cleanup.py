"""Branch / stash / orphan-DB cleanup helpers used by ``t3 workspace clean-all``.

Lives in its own module so :mod:`teatree.core.management.commands.workspace`
stays under the module-health LOC cap. Functions are kept private (``_``
prefix) because the only public surface is the ``clean-all`` subcommand.
"""

from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING

from teatree.core.cleanup.clean_ignore import is_clean_ignored
from teatree.core.cleanup.cleanup import _ref_captured_by_merge, _remote_tracking_ref_exists
from teatree.core.cleanup.cleanup_busy_guards import WorktreeBusyError, guard_live_worktree
from teatree.core.intake.resolve import match_worktree_by_path
from teatree.core.models import Worktree
from teatree.core.worktree.branch_classification import _branch_tree_matches_squash, is_squash_merged
from teatree.core.worktree.worktree_env import CACHE_DIRNAME, CACHE_FILENAME, write_env_cache
from teatree.utils import git
from teatree.utils.db import drop_db
from teatree.utils.run import CommandFailedError, run_allowed_to_fail, run_checked

if TYPE_CHECKING:
    from collections.abc import Callable

    from teatree.core.worktree.reconcile import Drift


# Regenerable artifacts a clean-working-tree probe must ignore: provisioning
# writes the env cache into every worktree (``worktree_env.write_env_cache``).
# In teatree's own clone these are gitignored, but an overlay repo may track
# them as untracked, so a porcelain status that lists only these is still
# "clean" for the gone-remote prune decision.
_REGENERABLE_WORKTREE_PATHS = (CACHE_FILENAME, f"{CACHE_DIRNAME}/")

# ``git status --porcelain`` prefixes each path with a two-char ``XY`` status
# code plus a space, e.g. ``?? path`` or `` M path``.
_PORCELAIN_STATUS_PREFIX_WIDTH = 3


def worktree_map(repo: str) -> dict[str, str]:
    """Return ``{branch_name: worktree_path}`` for active git worktrees."""
    raw = git.run(repo=repo, args=["worktree", "list", "--porcelain"])
    result: dict[str, str] = {}
    current_path = ""
    for line in raw.splitlines():
        if line.startswith("worktree "):
            current_path = line.removeprefix("worktree ")
        elif line.startswith("branch refs/heads/"):
            result[line.removeprefix("branch refs/heads/")] = current_path
    return result


def worktree_branches(repo: str) -> set[str]:
    """Return branch names linked to active git worktrees (safe to skip)."""
    return set(worktree_map(repo))


def _refuse_if_unpushed(repo: str, name: str, *, remote_ref_was_present: bool) -> str:
    """Return a refusal message when branch ``name`` has commits absent from all remotes (#706).

    Defense-in-depth for #710. ``_prune_squash_merged`` deletes a branch directly
    via ``git.branch_delete`` / ``git.worktree_remove``, bypassing the guarded
    teardown seam (``cleanup._raise_if_unpushed``) that ``Worktree``-row callers
    funnel through; that seam needs a ``Worktree`` instance this name-only path
    lacks, so the same primitive (``git.commits_absent_from_all_remotes``) is
    applied here directly.

    ``name`` is a local branch NAME resolvable in ``repo`` (the main clone), not
    the literal ``HEAD`` the cleanup seam probes: the only caller
    (``_prune_squash_merged`` via ``prune_branches``) enumerates names from
    ``git branch`` in this same ``repo`` after ``git worktree prune``.

    **Fetch precondition.** ``prune_branches`` runs ``git fetch --prune`` before
    reaching here, and samples ``remote_ref_was_present`` BEFORE that prune (a
    forge squash-merge leaves the source's tracking ref stale locally until the
    prune); a caller that has not fetched compares against a stale ``origin``.

    Genuinely squash-merged branches are not false-blocked: a positive merged
    signal (the pre-prune tracking ref, or the forge) AND a matching tree lets
    deletion proceed. A non-empty ``unpushed`` with NO merged-evidence means the
    commits exist on NO remote, so the branch is kept (#2205: tree equality alone
    is a false positive for a fully-reverted local branch). Fails closed: an
    inconclusive probe (``CommandFailedError``) refuses. Returns ``""`` when safe.
    """
    try:
        unpushed = git.commits_absent_from_all_remotes(repo, name)
    except CommandFailedError as exc:
        return (
            f"SKIPPED '{name}': could not verify the branch is pushed "
            f"(git probe failed: {exc}) — refusing to delete. Push the branch to keep the work."
        )
    if not unpushed:
        return ""
    if _ref_captured_by_merge(repo, name, name, remote_ref_was_present=remote_ref_was_present):
        return ""
    return (
        f"SKIPPED '{name}': {len(unpushed)} commit(s) on NO remote (data loss) — "
        f"refusing to delete. Push to a new branch to keep the work:\n  " + "\n  ".join(unpushed)
    )


def _prune_squash_merged(repo: str, name: str, wt_map: dict[str, str], *, remote_ref_was_present: bool) -> str:
    """Remove a confirmed squash-merged branch (and its worktree if linked).

    A branch whose tip tree matches the PR's merge commit is cleaned despite
    unsynced commits (typical for post-merge retro/docs work that is already
    captured by the squash).

    Honors the #706 data-loss guard (#710): even when the squash-merge
    heuristics say "clean", a branch whose commits are absent from every remote
    AND lacks merged-evidence is kept and a warning is returned — the safe
    default is never to destroy the only copy of work. ``remote_ref_was_present``
    is the caller's pre-prune sample of ``origin/<name>``'s tracking ref, the
    forge-CLI-free squash-merge signal threaded into the guard.
    """
    unsynced = git.unsynced_commits(repo, name)
    if unsynced and not _branch_tree_matches_squash(repo, name):
        return f"SKIPPED '{name}': {len(unsynced)} unsynced commit(s) — push to a new branch:\n  " + "\n  ".join(
            unsynced
        )
    refusal = _refuse_if_unpushed(repo, name, remote_ref_was_present=remote_ref_was_present)
    if refusal:
        return refusal
    wt_path = wt_map.get(name, "")
    if wt_path:
        git.worktree_remove(repo, wt_path)
        git.run(repo=repo, args=["worktree", "prune"])
    git.branch_delete(repo, name)
    return f"Pruned squash-merged branch: {name}"


class WorktreeReaper:
    """Workspace-scoped clean-all empty-ticket-dir pruning.

    Prunes the now-empty ticket dirs the done-worktree reaper leaves behind. The
    Worktree-row teardown itself is :func:`teatree.core.worktree.worktree_done.reap_done_worktrees`
    — the one consolidated done+redundant pass.
    """

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace

    def remove_empty_ticket_dirs(self) -> list[str]:
        """Remove ticket dirs that are empty or hold only empty repo subdirs.

        A multi-repo ticket dir (``ac/1234/`` with empty ``backend/`` +
        ``frontend/`` left behind after the worktrees are reaped) is not empty
        itself, so the single-level ``not any(iterdir())`` check kept it. This
        prunes empty leaf subdirs first, then the ticket dir if it is now empty.
        A subdir holding any real file or nested content is left untouched.

        The per-overlay WORKTREE root (``config.worktree_root()``) is resolved
        purely — it is created at the point of USE (ticket provisioning), not by
        the getter — so a fresh setup may have no dir yet. A missing root is "no
        ticket dirs to prune", never a crash.
        """
        removed: list[str] = []
        if not self.workspace.is_dir():
            return removed
        for entry in self.workspace.iterdir():
            if not entry.is_dir():
                continue
            for child in list(entry.iterdir()):
                if child.is_dir() and not any(child.iterdir()):
                    with suppress(OSError):
                        child.rmdir()
            if not any(entry.iterdir()):
                with suppress(OSError):
                    entry.rmdir()
                    removed.append(f"Removed empty dir: {entry.name}")
        return removed


def _worktree_clean(wt_path: str) -> bool:
    """Whether the worktree has no uncommitted changes (ignoring regenerable files).

    Provisioning seeds each worktree with the env cache; a status that lists only
    those regenerable artifacts is still clean for prune purposes. Any other dirty
    entry — staged, modified, or untracked real work — keeps the worktree.
    """
    if not Path(wt_path).is_dir():
        return False
    for line in git.status_porcelain(wt_path).splitlines():
        entry = line[_PORCELAIN_STATUS_PREFIX_WIDTH:].strip()
        if entry and not entry.startswith(_REGENERABLE_WORKTREE_PATHS):
            return False
    return True


def _prune_gone_worktree(repo: str, name: str, wt_path: str) -> str:
    """Remove the working tree of a gone-remote branch, keeping the branch ref.

    A branch whose ``origin/<branch>`` ref was pruned (squash-merged + deleted on
    origin) never appears as an ancestor of ``origin/main`` — the squash creates a
    new SHA — so the merged-ancestor and ``[gone]``-marker passes leave its
    worktree behind. This closes that gap.

    The operation is **non-destructive of commits**: only the on-disk working
    tree is removed (``git worktree remove``); the branch ref is deliberately
    kept, so the worktree is fully recoverable with ``git worktree add <path>
    <branch>`` and no committed work can be lost even if the gone-remote
    classification were wrong. This is why the by-SHA ``_refuse_if_unpushed``
    data-loss guard (which protects branch-ref *deletion*, and which a
    squash-merged branch always trips because the squash is a new SHA) is not
    applied here — we never delete the ref. The only loss a clean removal could
    cause is uncommitted working-tree changes, which the clean check forbids.

    As defense-in-depth a clean worktree is still kept when its branch carries
    commits ahead of ``origin/main`` that are not captured by a squash-merge —
    active post-merge work in progress. The probe is conservative in the safe
    direction: when it cannot confirm the content is merged it keeps the
    worktree, never removes it.

    Returns a one-line outcome — a removal, or a SKIPPED line when the worktree
    is kept (live work / uncommitted changes / genuinely-ahead work) or the
    removal failed.

    Liveness funnel (#291/#2243 rider): an OPPORTUNISTIC reaper must KEEP a
    worktree under live work, so this routes the row through
    :func:`guard_live_worktree` — the same liveness guard
    :func:`teatree.core.cleanup.cleanup.cleanup_worktree` fronts — before the removal. It
    deliberately does NOT call the full ``cleanup_worktree`` teardown, which would
    delete the branch ref this pass keeps for recoverability (and trip the #706
    data-loss refusal for a gone-remote squash branch with no forge CLI). A raw
    worktree with no ``Worktree`` row has no ticket liveness to consult.
    """
    row = match_worktree_by_path(wt_path)
    if row is not None:
        try:
            guard_live_worktree(row, respect_liveness=True, force=False)
        except WorktreeBusyError as exc:
            return f"SKIPPED '{name}': {exc}"
    if not _worktree_clean(wt_path):
        return f"SKIPPED '{name}': worktree has uncommitted changes — keeping {wt_path}"
    unsynced = git.unsynced_commits(repo, name)
    if unsynced and not _branch_tree_matches_squash(repo, name):
        return f"SKIPPED '{name}': {len(unsynced)} commit(s) ahead of origin/main — keeping {wt_path}"
    if git.worktree_remove(repo, wt_path):
        git.run(repo=repo, args=["worktree", "prune"])
        return f"Removed gone-remote worktree (branch kept): {name}"
    return f"SKIPPED '{name}': git worktree remove failed for {wt_path}"


def _prune_gone_remote_worktrees(repo: str, wt_map: dict[str, str], protected: set[str]) -> list[str]:
    """Reap worktrees of gone-remote branches; mark their refs protected.

    Worktree-linked branches whose ``origin/<branch>`` ref is gone (squash-merged
    + branch-deleted) are skipped by every branch-deletion pass — they have a live
    worktree. Reap the working tree (keep the branch ref) when it is clean and not
    genuinely ahead of ``origin/main``; otherwise keep both. Either way this pass
    is the authoritative handler for these branches, so it adds each one to
    ``protected`` (mutated in place): the later squash-merge pass would
    ``--force``-remove even a dirty worktree, and a reaped worktree is recoverable
    via ``git worktree add`` only while its ref survives.
    """
    cleaned: list[str] = []
    for name, wt_path in sorted(wt_map.items()):
        if name in protected or _remote_tracking_ref_exists(repo, name):
            continue
        cleaned.append(_prune_gone_worktree(repo, name, wt_path))
        protected.add(name)
    return cleaned


def prune_branches(repo: str) -> list[str]:
    """Delete local branches that are gone or merged, including squash-merged.

    ``clean_ignore``-matching branches (never-merge dev overrides, long-lived
    spikes) are added to ``protected`` up front via :func:`is_clean_ignored`, so
    every deletion pass below — gone-branch, merged-branch, and squash-merge —
    skips them through the single ``name in protected`` guard rather than each
    pass re-checking the globs.
    """
    cleaned: list[str] = []
    # Sample the remote tracking refs BEFORE the prune removes the stale ones: a
    # branch's prior tracking-ref presence is the forge-CLI-free proof it was once
    # pushed, threaded into the data-loss guard below.
    pre_prune_remote = git.run(repo=repo, args=["for-each-ref", "--format=%(refname:short)", "refs/remotes/origin"])
    pre_prune_remote_branches = {
        line.removeprefix("origin/")
        for line in pre_prune_remote.splitlines()
        if line.strip() not in {"", "origin/HEAD"}
    }
    git.run(repo=repo, args=["fetch", "--prune"])
    git.run(repo=repo, args=["worktree", "prune"])

    current = git.current_branch(repo)
    default = git.default_branch(repo)
    protected = {current, default, "main", "master"}
    wt_branches = worktree_branches(repo)

    wt_map = worktree_map(repo)

    all_local = {
        line.strip().removeprefix("* ").removeprefix("+ ")
        for line in git.run(repo=repo, args=["branch", "--no-color"]).splitlines()
    }
    protected |= {name for name in all_local if is_clean_ignored(name)}

    for line in git.run(repo=repo, args=["branch", "-v", "--no-color"]).splitlines():
        if "[gone]" not in line:
            continue
        name = line.strip().removeprefix("* ").removeprefix("+ ").split()[0]
        if name in protected or name in wt_branches:
            continue
        git.branch_delete(repo, name)
        cleaned.append(f"Pruned gone branch: {name}")

    cleaned.extend(_prune_gone_remote_worktrees(repo, wt_map, protected))

    for line in git.run(repo=repo, args=["branch", "--merged", f"origin/{default}", "--no-color"]).splitlines():
        name = line.strip().removeprefix("* ").removeprefix("+ ")
        if name in protected or name in wt_branches:
            continue
        git.branch_delete(repo, name)
        cleaned.append(f"Pruned merged branch: {name}")

    all_branches = {
        line.strip().removeprefix("* ").removeprefix("+ ")
        for line in git.run(repo=repo, args=["branch", "--no-color"]).splitlines()
    }
    for name in sorted(all_branches - protected):
        if not is_squash_merged(repo, name, default):
            continue
        cleaned.append(
            _prune_squash_merged(repo, name, wt_map, remote_ref_was_present=name in pre_prune_remote_branches)
        )

    remaining = {
        line.strip().removeprefix("* ").removeprefix("+ ")
        for line in git.run(repo=repo, args=["branch", "--no-color"]).splitlines()
    } - protected
    for name in sorted(remaining):
        commits = git.run(repo=repo, args=["rev-list", "--count", f"{default}..{name}"])
        cleaned.append(f"WARNING: branch '{name}' has {commits} unpushed commit(s) and no merged PR")

    return cleaned


def drop_orphan_databases() -> list[str]:
    """Drop Postgres databases matching wt_* that don't belong to any worktree."""
    from teatree.utils.db import pg_env, pg_host, pg_user  # noqa: PLC0415 — deferred: keeps command import light

    result = run_allowed_to_fail(
        ["psql", "-h", pg_host(), "-U", pg_user(), "-l", "-t", "-A"],
        env=pg_env(),
        expected_codes=None,
    )
    if result.returncode != 0:
        return []

    all_dbs = {line.split("|")[0] for line in result.stdout.splitlines() if line}
    wt_dbs = {db for db in all_dbs if db.startswith("wt_")}

    known_db_names = set(Worktree.objects.exclude(db_name="").values_list("db_name", flat=True))

    orphans = wt_dbs - known_db_names
    cleaned: list[str] = []
    for db_name in sorted(orphans):
        run_allowed_to_fail(
            ["dropdb", "-h", pg_host(), "-U", pg_user(), "--if-exists", db_name],
            env=pg_env(),
            expected_codes=None,
        )
        cleaned.append(f"Dropped orphan database: {db_name}")
    return cleaned


def _die(write_err: "Callable[[str], object]", message: str) -> None:
    """Write ``message`` to stderr then exit 1 — the #932 failure contract.

    Lives here (not in ``workspace``) only to keep that module under the
    module-health LOC cap; it has no cleanup-specific behaviour.
    """
    write_err(message)
    raise SystemExit(1)


def _raise_on_cleanup_failures(
    results: list[str],
    write_out: "Callable[[str], object]",
    write_err: "Callable[[str], object]",
) -> None:
    """Exit 1 if any genuinely-failed push/abandon line is in ``results``.

    A failed push/abandon must stop the caller (e.g. the followup loop):
    printing it and exiting 0 let cleanup look successful (#932). A
    ``Skipped:`` line is a benign no-op, not a failure.
    """
    failed = [r for r in results if r.startswith(("Push failed:", "Abandon failed:"))]
    if failed:
        for line in results:
            write_out(line)
        write_err(f"clean-all: {len(failed)} push/abandon failure(s).")
        raise SystemExit(1)


def _fix_drift(drift: "Drift") -> list[str]:
    """Apply reconciler fixes for one ticket's drift.

    Each fix uses :func:`run_checked` so failures surface — no silent
    swallow.  Called from ``t3 workspace doctor --fix``.
    """
    fixes: list[str] = []

    for c in drift.orphan_containers:
        run_checked(["docker", "rm", "-f", c.name])
        fixes.append(f"removed orphan container {c.name}")

    for d in drift.orphan_dbs:
        drop_db(d.db_name)
        fixes.append(f"dropped orphan DB {d.db_name}")

    for missing_wt in drift.missing_worktree_dirs:
        Worktree.objects.filter(pk=missing_wt.worktree_pk).update(extra={})
        fixes.append(f"cleared worktree_path on wt#{missing_wt.worktree_pk} (path gone: {missing_wt.path})")

    fixes.extend(
        f"stale worktree dir {stale.path} — remove manually with `git worktree remove`"
        for stale in drift.stale_worktree_dirs
    )

    for missing_cache in drift.missing_env_caches:
        wt = Worktree.objects.get(pk=missing_cache.worktree_pk)
        write_env_cache(wt)
        fixes.append(f"regenerated env cache for wt#{missing_cache.worktree_pk}")

    for cache_drift in drift.env_cache_drifts:
        wt = Worktree.objects.get(pk=cache_drift.worktree_pk)
        write_env_cache(wt)
        fixes.append(f"rewrote drifted env cache for wt#{cache_drift.worktree_pk}")

    fixes.extend(
        f"missing DB {m.db_name} for wt#{m.worktree_pk} — run `t3 <overlay> worktree provision` to re-provision"
        for m in drift.missing_dbs
    )

    fixes.extend(
        f"unresolvable overlay {u.overlay!r} on wt#{u.worktree_pk} — not installed here; "
        f"reinstall it or remove the row (its docker/DB can't be reconciled without the overlay)"
        for u in drift.unresolvable_overlays
    )

    return fixes
