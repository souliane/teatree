"""Workspace management: create ticket worktrees, finalize, clean stale branches."""

import os
import subprocess  # noqa: S404
import sys
from contextlib import suppress
from pathlib import Path

from django.conf import settings
from django_typer.management import TyperCommand, command

from teetree.core.models import Ticket, Worktree
from teetree.core.overlay_loader import get_overlay
from teetree.utils.git import default_branch
from teetree.utils.git import run as git_run


def _workspace_dir() -> Path:
    return Path(getattr(settings, "T3_WORKSPACE_DIR", os.environ.get("T3_WORKSPACE_DIR", Path.home() / "workspace")))


def _branch_prefix() -> str:
    return os.environ.get("T3_BRANCH_PREFIX", "dev")


def _create_git_worktree(workspace: Path, repo_name: str, ticket_dir: Path, branch: str) -> Path | None:
    """Run ``git worktree add`` for a single repo and return the worktree path."""
    repo_path = workspace / repo_name
    if not (repo_path / ".git").is_dir():
        print(f"  Skipping {repo_name}: not a git repository", file=sys.stderr)  # noqa: T201
        return None

    wt_path = ticket_dir / repo_name
    if wt_path.exists():
        print(f"  Skipping {repo_name}: {wt_path} already exists", file=sys.stderr)  # noqa: T201
        return wt_path

    # Pull latest before branching
    subprocess.run(["git", "pull", "--ff-only"], cwd=repo_path, capture_output=True, check=False)

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
    ) -> int:
        """Create a ticket with worktree entries for each affected repo."""
        overlay = get_overlay()
        repo_names = [r.strip() for r in repos.split(",") if r.strip()] if repos else overlay.get_workspace_repos()

        ticket = Ticket.objects.create(issue_url=issue_url, variant=variant, repos=repo_names)
        ticket.scope(issue_url=issue_url, variant=variant or None, repos=repo_names)
        ticket.save()

        workspace = _workspace_dir()
        prefix = _branch_prefix()
        first_repo = repo_names[0] if repo_names else "repo"
        branch = f"{prefix}-{first_repo}-{ticket.ticket_number}-ticket"
        ticket_dir = workspace / branch

        ticket_dir.mkdir(parents=True, exist_ok=True)

        for repo_name in repo_names:
            wt_path = _create_git_worktree(workspace, repo_name, ticket_dir, branch)
            worktree = Worktree.objects.create(
                ticket=ticket,
                repo_path=repo_name,
                branch=branch,
                extra={"worktree_path": str(wt_path)} if wt_path else {},
            )
            self.stdout.write(f"  {repo_name}: {'created' if wt_path else 'skipped'} (worktree #{worktree.pk})")

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
            wt_path = (worktree.extra or {}).get("worktree_path")
            cwd = wt_path or None
            default_br = default_branch(repo)
            try:
                git_run(repo=wt_path or repo, args=["fetch", "origin", default_br])

                # Squash: find merge-base, count commits, soft reset + recommit
                merge_base = subprocess.run(  # noqa: S603
                    ["git", "merge-base", f"origin/{default_br}", "HEAD"],
                    cwd=cwd,
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout.strip()
                commit_count = subprocess.run(  # noqa: S603
                    ["git", "rev-list", "--count", f"{merge_base}..HEAD"],
                    cwd=cwd,
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout.strip()
                if int(commit_count) > 1:
                    # Build squash message from existing commits or use provided message
                    if not message:
                        log = subprocess.run(  # noqa: S603
                            ["git", "log", "--oneline", f"{merge_base}..HEAD"],
                            cwd=cwd,
                            capture_output=True,
                            text=True,
                            check=True,
                        ).stdout.strip()
                        message = log.splitlines()[0] if log else f"Squash {commit_count} commits"
                    subprocess.run(  # noqa: S603
                        ["git", "reset", "--soft", merge_base],
                        cwd=cwd,
                        check=True,
                        capture_output=True,
                    )
                    subprocess.run(  # noqa: S603
                        ["git", "commit", "-m", message],
                        cwd=cwd,
                        check=True,
                        capture_output=True,
                    )
                    results.append(f"{repo}: squashed {commit_count} commits")
                else:
                    results.append(f"{repo}: single commit, no squash needed")

                git_run(repo=wt_path or repo, args=["rebase", f"origin/{default_br}"])
                results.append(f"{repo}: rebased on {default_br}")
            except subprocess.CalledProcessError as exc:
                results.append(f"{repo}: failed — {exc}")
        return "\n".join(results)

    @command(name="clean-all")
    def clean_all(self) -> list[str]:
        """Prune merged worktrees — remove git worktrees, drop DBs, clean directories."""
        cleaned: list[str] = []
        workspace = _workspace_dir()

        for worktree in Worktree.objects.filter(state=Worktree.State.CREATED):
            wt_path = (worktree.extra or {}).get("worktree_path", "")

            # Remove git worktree
            if wt_path:
                repo_main = workspace / worktree.repo_path
                if repo_main.is_dir():  # pragma: no branch
                    subprocess.run(  # noqa: S603
                        ["git", "worktree", "remove", "--force", wt_path],
                        cwd=repo_main,
                        capture_output=True,
                        check=False,
                    )
                    # Delete the branch
                    subprocess.run(  # noqa: S603
                        ["git", "branch", "-D", worktree.branch],
                        cwd=repo_main,
                        capture_output=True,
                        check=False,
                    )

            # Drop database
            if worktree.db_name:
                from teetree.utils.db import pg_env, pg_host, pg_user  # noqa: PLC0415

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
