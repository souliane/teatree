"""Branch / stash / orphan-DB cleanup helpers used by ``t3 workspace clean-all``.

Lives in its own module so :mod:`teatree.core.management.commands.workspace`
stays under the module-health LOC cap. Functions are kept private (``_``
prefix) because the only public surface is the ``clean-all`` subcommand.
"""

from contextlib import suppress
from fnmatch import fnmatch
from pathlib import Path
from typing import TYPE_CHECKING

from django.core.exceptions import ImproperlyConfigured

from teatree.config import get_effective_settings
from teatree.core.cleanup import (
    WorktreeBusyError,
    _branch_tree_matches_squash,
    _ref_captured_by_merge,
    _remote_tracking_ref_exists,
    cleanup_worktree,
)
from teatree.core.cleanup_busy_guards import guard_live_worktree
from teatree.core.clone_paths import resolve_clone_path
from teatree.core.management.commands._workspace_reap import reap_one_worktree
from teatree.core.models import Ticket, Worktree
from teatree.core.resolve import match_worktree_by_path
from teatree.core.worktree_env import CACHE_DIRNAME, CACHE_FILENAME, write_env_cache
from teatree.utils import git
from teatree.utils.db import drop_db
from teatree.utils.run import CommandFailedError, run_allowed_to_fail, run_checked

if TYPE_CHECKING:
    from collections.abc import Callable

    from teatree.core.reconcile import Drift
    from teatree.utils.run import CompletedProcess


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


def _run_host_cli(cmd: list[str], repo: str) -> "CompletedProcess[str] | None":
    """Run a host CLI that may be missing, returning ``None`` when it cannot run.

    ``gh`` / ``glab`` are optional — absent in CI without auth and blocked in
    sandboxes (a denied binary raises ``PermissionError``, a missing one
    ``FileNotFoundError``; both are ``OSError``). Swallowing ``OSError`` lets
    :func:`is_squash_merged` fall back to the diff check instead of crashing the
    whole ``clean-all`` run — the exact condition under which merged worktrees
    were left unpruned.
    """
    try:
        return run_allowed_to_fail(cmd, cwd=repo, expected_codes=None)
    except OSError:
        return None


def is_squash_merged(repo: str, branch: str, default: str) -> bool:
    # GitHub: ask if a PR for this branch was merged.
    result = _run_host_cli(
        ["gh", "pr", "list", "--head", branch, "--state", "merged", "--json", "number", "--limit", "1"],
        repo,
    )
    if result is not None and result.returncode == 0 and result.stdout.strip() not in {"", "[]"}:
        return True

    # GitLab: glab mr list output lines for found MRs start with "!" (e.g. "!5  Title  (branch)").
    result = _run_host_cli(
        ["glab", "mr", "list", "--merged", "--source-branch", branch, "--limit", "1"],
        repo,
    )
    if (
        result is not None
        and result.returncode == 0
        and any(line.lstrip().startswith("!") for line in result.stdout.splitlines())
    ):
        return True

    return _branch_captured_upstream(repo, branch, default)


def _branch_captured_upstream(repo: str, branch: str, default: str) -> bool:
    """Whether every unique commit of ``branch`` is already in ``origin/<default>``.

    The forge-CLI-free squash-merge signal. A squash-merge rewrites the source
    commits into one new SHA on the default branch, so ``branch`` is NOT an
    ancestor of ``origin/<default>`` and an is-ancestor / three-dot-diff test
    misses it. ``git cherry`` compares by patch-id instead: it prints ``- <sha>``
    for each ``branch`` commit whose change is already upstream (the squash
    captured it) and ``+ <sha>`` for one that is not. The branch is captured when
    cherry finds no ``+`` line — empty output (nothing unique) or every line a
    ``-`` (all unique commits are equivalent upstream). A probe failure (unknown
    ref, missing ``origin/<default>``) reads as not-captured so the data-loss
    guards downstream keep the worktree.
    """
    try:
        cherry = git.run(repo=repo, args=["cherry", f"origin/{default}", branch])
    except CommandFailedError:
        return False
    return all(line.startswith("-") for line in cherry.splitlines() if line.strip())


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


def is_clean_ignored(branch: str, *, overlay: str | None = None) -> bool:
    """Whether ``branch`` matches a ``clean_ignore`` glob and must never be reaped.

    The single predicate every clean-all deletion path consults — the worktree-row
    reaper, the CREATED-state row loop, and the branch-prune passes — so the
    never-reap guarantee lives in one place rather than three drifting copies.

    The DB-home ``clean_ignore`` setting is per-overlay overridable, so the
    patterns are resolved through :func:`get_effective_settings` for the row's
    own overlay (``overlay`` = ``worktree.overlay``) or, on the repo-scoped
    branch-prune path, the active overlay (``overlay=None``). Resolution per
    pattern set: the overlay-scope ``ConfigSetting`` row, then the global-scope
    row, then the empty default. A ``[teatree]`` / ``[overlays.<name>]`` TOML
    value is ignored on read.
    """
    patterns = get_effective_settings(overlay).clean_ignore
    return any(fnmatch(branch, pattern) for pattern in patterns)


class WorktreeReaper:
    """Workspace-scoped clean-all reaping: squash-merged rows + empty dirs.

    Groups the two passes that operate on the whole ``workspace`` (rather than a
    single repo like the branch/stash helpers): tear down Worktree rows whose
    branch shipped, then prune the now-empty ticket dirs they leave behind.
    """

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace

    def reap_squash_merged_worktrees(self, *, interactive: bool) -> list[str]:
        """Tear down Worktree rows whose branch is squash-merged.

        ``clean-all`` only reaped ``CREATED``-state rows; a PROVISIONED/READY row
        whose branch shipped under a retitled squash subject survived forever (its
        dir, compose project, and ticket dir all leaking). This pass reaps those.

        The squash signal reuses :func:`is_squash_merged` — the same forge-primary,
        empty-diff-fallback classifier the branch-prune pass uses (no duplicated
        subject-match logic). It is fail-safe to *not merged*: a missing forge CLI,
        a non-empty diff, or any uncertain outcome reads as keep, so an uncertain
        row is reported with a SKIPPED warning and never deleted (warn-not-fail).

        The teardown goes through :func:`reap_one_worktree` with the default
        ``strict_hygiene=True`` / ``force=False``, so the #706/#835/#1506 data-loss
        guards still refuse a branch with commits on no remote — a positive squash
        signal narrows the candidate set, it never bypasses the guards. Branches
        matching ``clean_ignore`` — resolved per the row's own overlay via
        :func:`is_clean_ignored` — are skipped before any classification.

        Every step that touches a possibly-corrupt sibling repo or an
        unregistered overlay is funnelled through :func:`reap_one_worktree` or the
        ``CommandFailedError`` guard below, so one bad row is skipped with a
        warning rather than aborting the whole ``clean-all`` run.
        """
        cleaned: list[str] = []
        for worktree in Worktree.objects.exclude(state=Worktree.State.CREATED).select_related("ticket"):
            if is_clean_ignored(worktree.branch, overlay=worktree.overlay):
                cleaned.append(f"SKIPPED '{worktree.branch}': matches clean_ignore — keeping")
                continue
            repo = resolve_clone_path(self.workspace, worktree)
            if repo is None or not repo.is_dir():
                continue
            try:
                default = git.default_branch(str(repo))
                merged = is_squash_merged(str(repo), worktree.branch, default)
            except (RuntimeError, CommandFailedError) as exc:
                cleaned.append(
                    f"SKIPPED '{worktree.branch}': could not classify against {repo} ({exc}) — keeping the row."
                )
                continue
            if not merged:
                continue
            # is_squash_merged already confirmed the branch shipped, so the
            # origin/main-relative hygiene gate is redundant here and would
            # re-refuse a genuinely squash-merged branch (distinct new SHA). The
            # always-on #706 data-loss guard still protects unpushed-everywhere work.
            cleaned.append(reap_one_worktree(worktree, interactive=interactive, strict_hygiene=False))
        return cleaned

    def remove_empty_ticket_dirs(self) -> list[str]:
        """Remove ticket dirs that are empty or hold only empty repo subdirs.

        A multi-repo ticket dir (``ac/1234/`` with empty ``backend/`` +
        ``frontend/`` left behind after the worktrees are reaped) is not empty
        itself, so the single-level ``not any(iterdir())`` check kept it. This
        prunes empty leaf subdirs first, then the ticket dir if it is now empty.
        A subdir holding any real file or nested content is left untouched.
        """
        removed: list[str] = []
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
    :func:`teatree.core.cleanup.cleanup_worktree` fronts — before the removal. It
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


# A ``git stash list`` line is ``stash@{N}: <subject>`` where git writes the
# subject as ``WIP on <branch>: ...`` (auto-stash) or ``On <branch>: ...``
# (``git stash push -m``). The branch is the text between the anchored
# ``[WIP ]on `` prefix and the first ``:`` that follows it. A naive split on
# ``" on "`` both missed the capital-``On`` (explicit-message) form entirely and
# mis-parsed any stash whose message contained the word "on", dropping stashes
# that still belonged to an existing branch.
def drop_orphan_databases() -> list[str]:
    """Drop Postgres databases matching wt_* that don't belong to any worktree."""
    from teatree.utils.db import pg_env, pg_host, pg_user  # noqa: PLC0415

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


def clean_merged_worktrees() -> list[str]:
    """Tear down every worktree whose ticket is already MERGED.

    An OPPORTUNISTIC sweep (the daily followup sync runs it), so it routes through
    :func:`cleanup_worktree`'s liveness guard: a merged ticket whose worktree has
    live work — a live session, an active/claimed task, an external-delivery
    lease, a recent E2E run, or an explicit pin — is KEPT, never torn down
    mid-task (#291/#2243). A targeted FSM teardown of a freshly-merged ticket is a
    separate path that bypasses liveness.

    Fails open per row: a worktree whose overlay is not installed in this
    environment is SKIPPED (recorded) rather than aborting the whole sweep
    (#2472); live work is SKIPPED (kept); any other teardown ``RuntimeError`` is
    reported as FAILED. Errors are surfaced inline — no suppression.
    """
    cleaned: list[str] = []
    for ticket in Ticket.objects.filter(state=Ticket.State.MERGED):
        for wt in Worktree.objects.filter(ticket=ticket):
            try:
                cleaned.append(str(cleanup_worktree(wt, strict_hygiene=False)))
            except ImproperlyConfigured as exc:
                cleaned.append(f"SKIPPED {wt.repo_path} ({wt.branch}): overlay not installed here — {exc}")
            except WorktreeBusyError as exc:
                cleaned.append(f"SKIPPED {wt.repo_path} ({wt.branch}): {exc}")
            except RuntimeError as exc:
                cleaned.append(f"FAILED {wt.repo_path} ({wt.branch}): {exc}")
    if not cleaned:
        return ["No merged tickets have lingering worktrees."]
    return cleaned
