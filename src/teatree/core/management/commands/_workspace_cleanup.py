"""Branch / stash / orphan-DB cleanup helpers used by ``t3 workspace clean-all``.

Lives in its own module so :mod:`teatree.core.management.commands.workspace`
stays under the module-health LOC cap. Functions are kept private (``_``
prefix) because the only public surface is the ``clean-all`` subcommand.
"""

import re
from contextlib import suppress
from fnmatch import fnmatch
from pathlib import Path
from typing import TYPE_CHECKING

from teatree.config import get_effective_settings
from teatree.core.cleanup import _branch_tree_matches_squash, cleanup_worktree
from teatree.core.clone_paths import resolve_clone_path
from teatree.core.models import Worktree
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

    diff = git.run(repo=repo, args=["diff", f"origin/{default}...{branch}", "--stat"])
    return not diff


def _refuse_if_unpushed(repo: str, name: str) -> str:
    """Return a refusal message when ``name`` has commits absent from all remotes (#706).

    Defense-in-depth for #710. ``_prune_squash_merged`` deletes a branch and its
    worktree directly via ``git.worktree_remove`` / ``git.branch_delete``,
    bypassing the guarded teardown seam (``cleanup._raise_if_unpushed``) that
    every ``Worktree``-row-driven caller funnels through. That seam needs a
    ``Worktree`` instance this name-only path does not have, so the same data-loss
    primitive (``git.commits_absent_from_all_remotes``) is applied here directly.

    This intentionally does **not** false-block genuinely squash-merged branches:
    once a branch's content is squash-merged and that squash is pushed, every
    branch commit is reachable from ``refs/remotes/origin/<default>`` so
    ``--not --remotes`` is empty. A non-empty result means the commits exist on
    NO remote — deleting the worktree would destroy the only copy.

    **Fails closed.** A failing probe (invalid/missing branch, corrupt repo)
    raises ``CommandFailedError``; we translate that into a refusal rather than
    proceeding, because an inconclusive probe cannot prove the work is pushed.
    Returns ``""`` when the branch is safe to delete.
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
    return (
        f"SKIPPED '{name}': {len(unpushed)} commit(s) on NO remote (data loss) — "
        f"refusing to delete. Push to a new branch to keep the work:\n  " + "\n  ".join(unpushed)
    )


def _prune_squash_merged(repo: str, name: str, wt_map: dict[str, str]) -> str:
    """Remove a confirmed squash-merged branch (and its worktree if linked).

    A branch whose tip tree matches the PR's merge commit is cleaned despite
    unsynced commits (typical for post-merge retro/docs work that is already
    captured by the squash).

    Honors the #706 data-loss guard (#710): even when the squash-merge
    heuristics say "clean", a branch whose commits are absent from every remote
    is kept and a warning is returned — the safe default is never to destroy the
    only copy of work.
    """
    unsynced = git.unsynced_commits(repo, name)
    if unsynced and not _branch_tree_matches_squash(repo, name):
        return f"SKIPPED '{name}': {len(unsynced)} unsynced commit(s) — push to a new branch:\n  " + "\n  ".join(
            unsynced
        )
    refusal = _refuse_if_unpushed(repo, name)
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

    ``clean_ignore`` is per-overlay overridable, so the patterns are resolved
    through :func:`get_effective_settings` for the row's own overlay (``overlay``
    = ``worktree.overlay``) or, on the repo-scoped branch-prune path, the active
    overlay (``overlay=None``). Resolution per pattern set: ``[overlays.<name>]``
    override, global ``[teatree] clean_ignore``, empty default.
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

        The teardown goes through :func:`cleanup_worktree` with the default
        ``strict_hygiene=True`` / ``force=False``, so the #706/#835/#1506 data-loss
        guards still refuse a branch with commits on no remote — a positive squash
        signal narrows the candidate set, it never bypasses the guards. Branches
        matching ``clean_ignore`` — resolved per the row's own overlay via
        :func:`is_clean_ignored` — are skipped before any classification.
        """
        cleaned: list[str] = []
        for worktree in Worktree.objects.exclude(state=Worktree.State.CREATED).select_related("ticket"):
            if is_clean_ignored(worktree.branch, overlay=worktree.overlay):
                cleaned.append(f"SKIPPED '{worktree.branch}': matches clean_ignore — keeping")
                continue
            repo = resolve_clone_path(self.workspace, worktree)
            if repo is None or not repo.is_dir():
                continue
            default = git.default_branch(str(repo))
            if not is_squash_merged(str(repo), worktree.branch, default):
                continue
            try:
                cleaned.append(str(cleanup_worktree(worktree)))
            except RuntimeError as exc:
                cleaned.append(resolve_unsynced_worktree(worktree, exc, interactive=interactive))
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


def _origin_ref_exists(repo: str, branch: str) -> bool:
    """Whether ``refs/remotes/origin/<branch>`` still exists after ``fetch --prune``.

    A branch whose remote tracking ref is gone is "gone-remote" — the branch was
    deleted on origin, the normal terminal state of a squash-merged PR (the merge
    creates a new commit on the default branch and deletes the source ref). A
    branch still on origin is open work and must be kept.
    """
    return git.check(repo=repo, args=["show-ref", "--verify", "--quiet", f"refs/remotes/origin/{branch}"])


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
    is kept (uncommitted changes / genuinely-ahead work) or the removal failed.
    """
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
        if name in protected or _origin_ref_exists(repo, name):
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
        cleaned.append(_prune_squash_merged(repo, name, wt_map))

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
_STASH_BRANCH_RE = re.compile(r"^stash@\{\d+\}:\s+(?:WIP on|On)\s+(?P<branch>[^:]+):")


def _stash_branch(line: str) -> str:
    """Return the branch a ``git stash list`` line belongs to, or ``""`` if unparsable.

    A stash taken on a detached HEAD reads ``On (no branch): ...`` — there is no
    owning branch to compare against, so it is reported as unparsable and the
    stash is kept rather than reaped.
    """
    match = _STASH_BRANCH_RE.match(line)
    if not match:
        return ""
    branch = match.group("branch").strip()
    return "" if branch == "(no branch)" else branch


def drop_orphaned_stashes(repo: str) -> list[str]:
    """Drop stashes whose branch no longer exists."""
    stash_list = git.run(repo=repo, args=["stash", "list"])
    if not stash_list:
        return []

    existing = {
        line.strip().removeprefix("* ").removeprefix("+ ")
        for line in git.run(repo=repo, args=["branch", "--no-color"]).splitlines()
    }

    cleaned: list[str] = []
    entries = stash_list.splitlines()
    for i in range(len(entries) - 1, -1, -1):
        line = entries[i]
        branch = _stash_branch(line)
        if not branch or branch in existing:
            continue
        git.run(repo=repo, args=["stash", "drop", f"stash@{{{i}}}"])
        cleaned.append(f"Dropped orphaned stash: {line.split(':')[0]} (was on {branch})")

    return cleaned


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


def resolve_unsynced_worktree(worktree: Worktree, exc: RuntimeError, *, interactive: bool) -> str:
    """Decide what to do with a worktree whose branch has genuinely-unpushed work."""
    if not interactive:
        return f"Skipped: {exc}"

    prompt = (
        f"\n{worktree.repo_path} ({worktree.branch}) — genuinely unpushed work.\n"
        f"  {exc}\n"
        "  [P]ush to remote / [A]bandon (force delete) / [S]kip (default): "
    )
    try:
        choice = input(prompt).strip().lower()
    except EOFError:
        return f"Skipped: {exc}"

    if choice == "p":
        return push_unsynced_branch(worktree)
    if choice == "a":
        return abandon_unsynced_branch(worktree)
    return f"Skipped: {exc}"


def push_unsynced_branch(worktree: Worktree) -> str:
    wt_path = (worktree.extra or {}).get("worktree_path", "")
    if not wt_path or not Path(wt_path).is_dir():
        return f"Push failed: {worktree.repo_path} ({worktree.branch}) — worktree path missing"
    result = run_allowed_to_fail(
        ["git", "-C", wt_path, "push", "-u", "origin", worktree.branch],
        expected_codes=None,
    )
    if result.returncode != 0:
        return f"Push failed: {worktree.repo_path} ({worktree.branch}) — {result.stderr.strip()}"
    overlay_name = worktree.ticket.overlay or "<overlay>"
    return (
        f"Pushed: {worktree.repo_path} ({worktree.branch}). "
        f"Run `t3 {overlay_name} pr create {worktree.ticket.pk}` to open a PR."
    )


def abandon_unsynced_branch(worktree: Worktree) -> str:
    try:
        return str(cleanup_worktree(worktree, force=True))
    except Exception as exc:  # noqa: BLE001
        return f"Abandon failed: {worktree.repo_path} ({worktree.branch}) — {exc}"


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

    return fixes
