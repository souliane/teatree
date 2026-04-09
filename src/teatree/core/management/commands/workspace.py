"""Workspace management: create ticket worktrees, finalize, clean stale branches."""

import os
import re
import subprocess  # noqa: S404
import sys
from contextlib import suppress
from pathlib import Path

import typer
from django_typer.management import TyperCommand, command

from teatree.config import load_config
from teatree.core.cleanup import cleanup_worktree
from teatree.core.models import Ticket, Worktree
from teatree.core.overlay_loader import get_overlay
from teatree.utils import git


def _worktree_map(repo: str) -> dict[str, str]:
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


def _worktree_branches(repo: str) -> set[str]:
    """Return branch names linked to active git worktrees (safe to skip)."""
    return set(_worktree_map(repo))


def _is_squash_merged(repo: str, branch: str, default: str) -> bool:
    """Check if *branch* was squash-merged into *default*.

    A squash-merge rewrites history so ``git branch --merged`` won't detect it.

    Strategy: merge the branch into a temporary tree-only merge with main.
    If ``git merge-tree`` reports no conflicts and ``git cherry`` shows all
    commits as applied, the branch is merged. As a fast fallback, check if
    ``gh pr list`` reports the branch's PR as merged.
    """
    # Fast path: ask GitHub if a PR for this branch was merged.
    result = subprocess.run(  # noqa: S603
        ["gh", "pr", "list", "--head", branch, "--state", "merged", "--json", "number", "--limit", "1"],
        capture_output=True,
        text=True,
        check=False,
        cwd=repo,
    )
    if result.returncode == 0 and result.stdout.strip() not in {"", "[]"}:
        return True

    # Fallback: empty diff means all changes are already in main.
    diff = git.run(repo=repo, args=["diff", f"origin/{default}...{branch}", "--stat"])
    return not diff


def _prune_branches(repo: str) -> list[str]:
    """Delete local branches that are gone or merged.

    Handles squash-merged branches by comparing tree content against main.
    Prunes stale git worktree entries before checking branches so that
    worktrees whose directories no longer exist don't block cleanup.
    """
    cleaned: list[str] = []
    git.run(repo=repo, args=["fetch", "--prune"])
    git.run(repo=repo, args=["worktree", "prune"])

    current = git.current_branch(repo)
    default = git.default_branch(repo)
    protected = {current, default, "main", "master"}
    wt_branches = _worktree_branches(repo)

    wt_map = _worktree_map(repo)

    # Pass 1: delete "gone" branches that are not worktree-linked.
    for line in git.run(repo=repo, args=["branch", "-v", "--no-color"]).splitlines():
        if "[gone]" not in line:
            continue
        name = line.strip().removeprefix("+ ").split()[0]
        if name in protected or name in wt_branches:
            continue
        git.branch_delete(repo, name)
        cleaned.append(f"Pruned gone branch: {name}")

    # Pass 2: delete branches merged via regular merge.
    for line in git.run(repo=repo, args=["branch", "--merged", f"origin/{default}", "--no-color"]).splitlines():
        name = line.strip().removeprefix("* ").removeprefix("+ ")
        if name in protected or name in wt_branches:
            continue
        git.branch_delete(repo, name)
        cleaned.append(f"Pruned merged branch: {name}")

    # Pass 3: detect squash-merged branches (worktree-linked or not).
    # Squash-merge rewrites history so --merged can't detect them.
    # Uses the GitHub API as the primary signal, falls back to diff comparison.
    all_branches = {
        line.strip().removeprefix("* ").removeprefix("+ ")
        for line in git.run(repo=repo, args=["branch", "--no-color"]).splitlines()
    }
    for name in sorted(all_branches - protected):
        if not _is_squash_merged(repo, name, default):
            continue
        wt_path = wt_map.get(name, "")
        if wt_path:
            git.worktree_remove(repo, wt_path)
            git.run(repo=repo, args=["worktree", "prune"])
        git.branch_delete(repo, name)
        cleaned.append(f"Pruned squash-merged branch: {name}")

    # Pass 4: warn about remaining non-protected branches with no merged PR.
    # Re-read after deletions above.
    remaining = {
        line.strip().removeprefix("* ").removeprefix("+ ")
        for line in git.run(repo=repo, args=["branch", "--no-color"]).splitlines()
    } - protected
    for name in sorted(remaining):
        commits = git.run(repo=repo, args=["rev-list", "--count", f"{default}..{name}"])
        cleaned.append(f"WARNING: branch '{name}' has {commits} unpushed commit(s) and no merged PR")

    return cleaned


def _drop_orphaned_stashes(repo: str) -> list[str]:
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


def _drop_orphan_databases() -> list[str]:
    """Drop Postgres databases matching wt_* that don't belong to any worktree."""
    from teatree.utils.db import pg_env, pg_host, pg_user  # noqa: PLC0415

    result = subprocess.run(  # noqa: S603
        ["psql", "-h", pg_host(), "-U", pg_user(), "-l", "-t", "-A"],
        env=pg_env(),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return []

    all_dbs = {line.split("|")[0] for line in result.stdout.splitlines() if line}
    wt_dbs = {db for db in all_dbs if db.startswith("wt_")}

    known_db_names = set(Worktree.objects.exclude(db_name="").values_list("db_name", flat=True))

    orphans = wt_dbs - known_db_names
    cleaned: list[str] = []
    for db_name in sorted(orphans):
        subprocess.run(  # noqa: S603
            ["dropdb", "-h", pg_host(), "-U", pg_user(), "--if-exists", db_name],
            env=pg_env(),
            capture_output=True,
            check=False,
        )
        cleaned.append(f"Dropped orphan database: {db_name}")
    return cleaned


def _workspace_dir() -> Path:
    return load_config().user.workspace_dir


def _branch_prefix() -> str:
    prefix = os.environ.get("T3_BRANCH_PREFIX", "")
    if not prefix:
        name = git.run(args=["config", "user.name"])
        if name:
            prefix = "".join(word[0].lower() for word in name.split() if word)
    return prefix or "dev"


_WORKTREE_SKIPPED = Path("/dev/null/.skipped")  # sentinel: repo skipped, not a failure


def _slugify(text: str, max_length: int = 40) -> str:
    """Convert text to a URL-safe slug for branch names."""
    return re.sub(r"[^a-z0-9]+", "-", text.strip().lower()).strip("-")[:max_length]


def _build_branch_name(repo_names: list[str], ticket_number: str, description: str) -> str:
    """Build the git branch name from repo list, ticket number, and description."""
    prefix = _branch_prefix()
    first_repo = Path(repo_names[0]).name if repo_names else "repo"
    slug = _slugify(description) if description else "ticket"
    return f"{prefix}-{first_repo}-{ticket_number}-{slug}"


def _create_git_worktree(workspace: Path, repo_name: str, ticket_dir: Path, branch: str) -> Path | None:
    """Run ``git worktree add`` for a single repo and return the worktree path.

    ``repo_name`` may be a nested path relative to ``workspace`` (e.g.
    ``souliane/teatree``).  The worktree subdirectory uses the basename
    (``teatree``) to keep the ticket directory flat.

    Returns ``_WORKTREE_SKIPPED`` when the repo doesn't exist or has no ``.git``,
    the existing ``wt_path`` when it already exists, and ``None`` on actual failure.

    When ``git worktree add -b`` fails because the branch already exists
    (e.g. partial failure recovery), retries without ``-b`` to reuse the
    existing branch.
    """
    repo_path = workspace / repo_name
    if not (repo_path / ".git").is_dir():
        print(f"  Skipping {repo_name}: not a git repository", file=sys.stderr)  # noqa: T201
        return _WORKTREE_SKIPPED

    wt_path = ticket_dir / Path(repo_name).name
    if wt_path.exists():
        print(f"  Skipping {repo_name}: {wt_path} already exists", file=sys.stderr)  # noqa: T201
        return wt_path

    # Pull latest before branching
    git.pull_ff_only(str(repo_path))

    success = git.worktree_add(str(repo_path), str(wt_path), branch, create_branch=True)
    if not success:
        # Retry without -b if branch already exists (partial failure recovery)
        success = git.worktree_add(str(repo_path), str(wt_path), branch, create_branch=False)
    if not success:
        sys.stderr.write(f"  Error creating worktree for {repo_name}\n")
        return None

    # Symlink .python-version from main repo
    pv = repo_path / ".python-version"
    pv_dest = wt_path / ".python-version"
    if pv.is_file() and not pv_dest.exists():
        with suppress(OSError):
            pv_dest.symlink_to(pv)

    return wt_path


class Command(TyperCommand):
    @command()
    def ticket(
        self,
        issue_url: str,
        variant: str = "",
        repos: str = "",
        description: str = "",
    ) -> int:
        """Create or update a ticket with worktree entries for each affected repo.

        Idempotent: safe to re-run after partial failures. Existing worktrees
        are skipped, missing repos are added, and failed entries are cleaned up.
        """
        overlay = get_overlay()
        repo_names = [r.strip() for r in repos.split(",") if r.strip()] if repos else overlay.get_workspace_repos()

        ticket, _created = Ticket.objects.get_or_create(
            issue_url=issue_url,
            defaults={"variant": variant, "repos": repo_names},
        )
        if ticket.state == Ticket.State.NOT_STARTED:
            ticket.scope(issue_url=issue_url, variant=variant or None, repos=repo_names)
        # Merge new repos into existing ticket (preserves order, deduplicates)
        ticket.repos = list(dict.fromkeys((ticket.repos or []) + repo_names))
        ticket.save()

        if not description:
            description = overlay.metadata.get_issue_title(issue_url)
        workspace = _workspace_dir()
        branch = _build_branch_name(repo_names, ticket.ticket_number, description)
        ticket_dir = workspace / branch

        ticket_dir.mkdir(parents=True, exist_ok=True)

        new_worktrees: list[Worktree] = []
        failed_worktrees: list[Worktree] = []
        for repo_name in repo_names:
            worktree, wt_created = Worktree.objects.get_or_create(
                ticket=ticket,
                repo_path=repo_name,
                defaults={"branch": branch},
            )
            if not wt_created:
                self.stdout.write(f"  {repo_name}: already tracked (worktree #{worktree.pk})")
                continue

            wt_path = _create_git_worktree(workspace, repo_name, ticket_dir, branch)
            is_real_path = wt_path is not None and wt_path != _WORKTREE_SKIPPED
            if is_real_path:
                worktree.extra = {"worktree_path": str(wt_path)}
                worktree.save(update_fields=["extra"])
            new_worktrees.append(worktree)
            if wt_path is None:
                failed_worktrees.append(worktree)
            self.stdout.write(f"  {repo_name}: {'created' if is_real_path else 'skipped'} (worktree #{worktree.pk})")

        # Clean up DB entries for repos that actually failed
        for wt in failed_worktrees:
            wt.delete()
            new_worktrees.remove(wt)

        # Full rollback only when ALL new worktrees failed and no pre-existing ones
        if failed_worktrees and not new_worktrees and not ticket.worktrees.exists():
            self.stderr.write("  All worktree creations failed — rolling back ticket.")
            ticket.delete()
            with suppress(OSError):
                ticket_dir.rmdir()
            return 0

        if failed_worktrees:
            names = [wt.repo_path for wt in failed_worktrees]
            self.stderr.write(f"  WARNING: failed to create worktrees for: {', '.join(names)}")

        self.stdout.write(f"\nTicket #{ticket.pk} — worktrees in {ticket_dir}")
        self.stdout.write(f"  Branch: {branch}")
        return int(ticket.pk)

    @command()
    def finalize(self, ticket_id: int, *, message: str = "") -> str:
        """Squash worktree commits into one, then rebase on the default branch."""
        ticket = Ticket.objects.get(pk=ticket_id)
        results: list[str] = []
        for worktree in ticket.worktrees.all():
            repo = worktree.repo_path
            repo_dir = (worktree.extra or {}).get("worktree_path") or repo
            default_br = git.default_branch(repo)
            try:
                status = git.status_porcelain(repo_dir)
                if status:
                    results.append(f"{repo}: SKIPPED — uncommitted changes:\n{status}")
                    continue

                git.fetch(repo_dir, "origin", default_br)

                base = git.merge_base(repo_dir, f"origin/{default_br}")
                count = git.rev_count(repo_dir, f"{base}..HEAD")
                log = git.log_oneline(repo_dir, f"{base}..HEAD")
                if log:
                    self.stdout.write(f"  {repo} commits ({count}):\n    " + "\n    ".join(log.splitlines()))

                if count > 1:
                    if not message:
                        message = log.splitlines()[0] if log else f"Squash {count} commits"
                    git.soft_reset(repo_dir, base)
                    git.commit(repo_dir, message)
                    results.append(f"{repo}: squashed {count} commits")
                else:
                    results.append(f"{repo}: single commit, no squash needed")

                git.rebase(repo_dir, f"origin/{default_br}")
                results.append(f"{repo}: rebased on {default_br}")
            except subprocess.CalledProcessError as exc:
                results.extend(
                    [
                        f"{repo}: rebase failed — {exc}",
                        f"  To abort: git -C {repo_dir} rebase --abort",
                        f"  To resolve: fix conflicts, git add, then: git -C {repo_dir} rebase --continue",
                    ]
                )
        return "\n".join(results)

    @command(name="clean-all")
    def clean_all(
        self,
        keep_dslr: int = typer.Option(1, help="Number of DSLR snapshots to keep per tenant."),
    ) -> list[str]:
        """Prune merged worktrees, stale branches, orphaned stashes, orphan databases, and old DSLR snapshots."""
        workspace = _workspace_dir()
        cleaned = [cleanup_worktree(wt) for wt in Worktree.objects.filter(state=Worktree.State.CREATED)]

        for entry in workspace.iterdir():
            if entry.is_dir() and not any(entry.iterdir()):
                with suppress(OSError):
                    entry.rmdir()
                    cleaned.append(f"Removed empty dir: {entry.name}")

        cleaned.extend(_drop_orphan_databases())

        repo_root = Path.cwd()
        if (repo_root / ".git").exists():
            cleaned.extend(_prune_branches(str(repo_root)))
            cleaned.extend(_drop_orphaned_stashes(str(repo_root)))

        # Prune old DSLR snapshots
        from teatree.utils.django_db import prune_dslr_snapshots  # noqa: PLC0415

        pruned = prune_dslr_snapshots(keep=keep_dslr)
        cleaned.extend(f"Pruned DSLR snapshot: {name}" for name in pruned)

        return cleaned
