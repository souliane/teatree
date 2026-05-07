"""Branch / stash / orphan-DB cleanup helpers used by ``t3 workspace clean-all``.

Lives in its own module so :mod:`teatree.core.management.commands.workspace`
stays under the module-health LOC cap. Functions are kept private (``_``
prefix) because the only public surface is the ``clean-all`` subcommand.
"""

from pathlib import Path

from teatree.core.cleanup import _branch_tree_matches_squash, cleanup_worktree
from teatree.core.models import Worktree
from teatree.utils import git
from teatree.utils.run import run_allowed_to_fail


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


def is_squash_merged(repo: str, branch: str, default: str) -> bool:
    # GitHub: ask if a PR for this branch was merged.
    result = run_allowed_to_fail(
        ["gh", "pr", "list", "--head", branch, "--state", "merged", "--json", "number", "--limit", "1"],
        cwd=repo,
        expected_codes=None,
    )
    if result.returncode == 0 and result.stdout.strip() not in {"", "[]"}:
        return True

    # GitLab: glab mr list output lines for found MRs start with "!" (e.g. "!5  Title  (branch)").
    result = run_allowed_to_fail(
        ["glab", "mr", "list", "--merged", "--source-branch", branch, "--limit", "1"],
        cwd=repo,
        expected_codes=None,
    )
    if result.returncode == 0 and any(line.lstrip().startswith("!") for line in result.stdout.splitlines()):
        return True

    diff = git.run(repo=repo, args=["diff", f"origin/{default}...{branch}", "--stat"])
    return not diff


def prune_squash_merged(repo: str, name: str, wt_map: dict[str, str]) -> str:
    """Remove a confirmed squash-merged branch (and its worktree if linked).

    A branch whose tip tree matches the PR's merge commit is cleaned despite
    unsynced commits (typical for post-merge retro/docs work that is already
    captured by the squash).
    """
    unsynced = git.unsynced_commits(repo, name)
    if unsynced and not _branch_tree_matches_squash(repo, name):
        return f"SKIPPED '{name}': {len(unsynced)} unsynced commit(s) — push to a new branch:\n  " + "\n  ".join(
            unsynced
        )
    wt_path = wt_map.get(name, "")
    if wt_path:
        git.worktree_remove(repo, wt_path)
        git.run(repo=repo, args=["worktree", "prune"])
    git.branch_delete(repo, name)
    return f"Pruned squash-merged branch: {name}"


def prune_branches(repo: str) -> list[str]:
    """Delete local branches that are gone or merged, including squash-merged."""
    cleaned: list[str] = []
    git.run(repo=repo, args=["fetch", "--prune"])
    git.run(repo=repo, args=["worktree", "prune"])

    current = git.current_branch(repo)
    default = git.default_branch(repo)
    protected = {current, default, "main", "master"}
    wt_branches = worktree_branches(repo)

    wt_map = worktree_map(repo)

    for line in git.run(repo=repo, args=["branch", "-v", "--no-color"]).splitlines():
        if "[gone]" not in line:
            continue
        name = line.strip().removeprefix("+ ").split()[0]
        if name in protected or name in wt_branches:
            continue
        git.branch_delete(repo, name)
        cleaned.append(f"Pruned gone branch: {name}")

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
        cleaned.append(prune_squash_merged(repo, name, wt_map))

    remaining = {
        line.strip().removeprefix("* ").removeprefix("+ ")
        for line in git.run(repo=repo, args=["branch", "--no-color"]).splitlines()
    } - protected
    for name in sorted(remaining):
        commits = git.run(repo=repo, args=["rev-list", "--count", f"{default}..{name}"])
        cleaned.append(f"WARNING: branch '{name}' has {commits} unpushed commit(s) and no merged PR")

    return cleaned


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
        if " on " not in line:
            continue
        branch_part = line.split(" on ", 1)[1].split(":")[0].strip()
        if branch_part not in existing:
            git.run(repo=repo, args=["stash", "drop", f"stash@{{{i}}}"])
            cleaned.append(f"Dropped orphaned stash: {line.split(':')[0]} (was on {branch_part})")

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
        return cleanup_worktree(worktree, force=True)
    except Exception as exc:  # noqa: BLE001
        return f"Abandon failed: {worktree.repo_path} ({worktree.branch}) — {exc}"
