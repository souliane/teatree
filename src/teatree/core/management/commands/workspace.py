"""Workspace management: create ticket worktrees, finalize, clean stale branches."""

import os
import subprocess  # noqa: S404
import sys
from contextlib import suppress
from pathlib import Path

from django_typer.management import TyperCommand, command

from teatree.config import load_config
from teatree.core.models import Ticket, Worktree
from teatree.core.overlay_loader import get_overlay
from teatree.utils import git


def _workspace_dir() -> Path:
    return load_config().user.workspace_dir


def _worktrees_dir() -> Path:
    return load_config().user.worktrees_dir


def _branch_prefix() -> str:
    prefix = os.environ.get("T3_BRANCH_PREFIX", "")
    if not prefix:
        name = git.run(args=["config", "user.name"])
        if name:
            prefix = "".join(word[0].lower() for word in name.split() if word)
    return prefix or "dev"


_WORKTREE_SKIPPED = Path("/dev/null/.skipped")  # sentinel: repo skipped, not a failure


def _create_git_worktree(workspace: Path, repo_name: str, ticket_dir: Path, branch: str) -> Path | None:
    """Run ``git worktree add`` for a single repo and return the worktree path.

    Returns ``_WORKTREE_SKIPPED`` when the repo doesn't exist or has no ``.git``,
    the existing ``wt_path`` when it already exists, and ``None`` on actual failure.
    """
    repo_path = workspace / repo_name
    if not (repo_path / ".git").is_dir():
        print(f"  Skipping {repo_name}: not a git repository", file=sys.stderr)  # noqa: T201
        return _WORKTREE_SKIPPED

    wt_path = ticket_dir / repo_name
    if wt_path.exists():
        print(f"  Skipping {repo_name}: {wt_path} already exists", file=sys.stderr)  # noqa: T201
        return wt_path

    # Pull latest before branching
    git.pull_ff_only(str(repo_path))

    result = subprocess.run(  # noqa: S603
        ["git", "worktree", "add", "-b", branch, str(wt_path)],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        print(f"  Error creating worktree for {repo_name}: {result.stderr.strip()}", file=sys.stderr)  # noqa: T201
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
        """Create a ticket with worktree entries for each affected repo."""
        overlay = get_overlay()
        repo_names = [r.strip() for r in repos.split(",") if r.strip()] if repos else overlay.get_workspace_repos()

        ticket, _created = Ticket.objects.get_or_create(
            issue_url=issue_url,
            defaults={"variant": variant, "repos": repo_names},
        )
        if ticket.state == Ticket.State.NOT_STARTED:
            ticket.scope(issue_url=issue_url, variant=variant or None, repos=repo_names)
        ticket.save()

        workspace = _workspace_dir()
        prefix = _branch_prefix()
        first_repo = repo_names[0] if repo_names else "repo"
        slug = description.strip().lower().replace(" ", "-")[:40] if description else "ticket"
        branch = f"{prefix}-{first_repo}-{ticket.ticket_number}-{slug}"
        ticket_dir = workspace / branch

        ticket_dir.mkdir(parents=True, exist_ok=True)

        created_worktrees: list[Worktree] = []
        failures = 0
        for repo_name in repo_names:
            wt_path = _create_git_worktree(workspace, repo_name, ticket_dir, branch)
            is_real_path = wt_path is not None and wt_path != _WORKTREE_SKIPPED
            worktree = Worktree.objects.create(
                ticket=ticket,
                repo_path=repo_name,
                branch=branch,
                extra={"worktree_path": str(wt_path)} if is_real_path else {},
            )
            created_worktrees.append(worktree)
            if wt_path is None:
                failures += 1
            self.stdout.write(f"  {repo_name}: {'created' if is_real_path else 'skipped'} (worktree #{worktree.pk})")

        if failures == len(repo_names):
            self.stderr.write("  All worktree creations failed — rolling back ticket and DB entries.")
            for wt in created_worktrees:
                wt.delete()
            ticket.delete()
            with suppress(OSError):
                ticket_dir.rmdir()
            return 0

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
                results.append(f"{repo}: failed — {exc}")
        return "\n".join(results)

    @command(name="clean-all")
    def clean_all(self) -> list[str]:
        """Prune merged worktrees — remove git worktrees, drop DBs, clean directories."""
        cleaned: list[str] = []
        workspace = _workspace_dir()
        overlay = get_overlay()

        for worktree in Worktree.objects.filter(state=Worktree.State.CREATED):
            wt_path = (worktree.extra or {}).get("worktree_path", "")

            # Dirty-state check: warn if uncommitted changes
            if wt_path and Path(wt_path).is_dir():
                status = git.status_porcelain(wt_path)
                if status:
                    self.stderr.write(f"  WARNING: {worktree.repo_path} has uncommitted changes")

            # Run overlay-specific cleanup (Docker teardown, etc.)
            for step in overlay.get_cleanup_steps(worktree):
                with suppress(Exception):
                    step.callable()

            # Remove git worktree
            if wt_path:
                repo_main = workspace / worktree.repo_path
                if repo_main.is_dir():  # pragma: no branch
                    git.worktree_remove(str(repo_main), wt_path)
                    git.branch_delete(str(repo_main), worktree.branch)

            # Drop database
            if worktree.db_name:
                from teatree.utils.db import pg_env, pg_host, pg_user  # noqa: PLC0415

                subprocess.run(  # noqa: S603
                    ["dropdb", "-h", pg_host(), "-U", pg_user(), "--if-exists", worktree.db_name],
                    env=pg_env(),
                    capture_output=True,
                    check=False,
                )

            cleaned.append(f"Cleaned: {worktree.repo_path} ({worktree.branch})")
            worktree.delete()

        # Remove empty ticket directories
        for entry in workspace.iterdir():
            if entry.is_dir() and not any(entry.iterdir()):
                with suppress(OSError):
                    entry.rmdir()
                    cleaned.append(f"Removed empty dir: {entry.name}")

        return cleaned
