import logging
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, cast

from teatree.config import workspace_dir as _workspace_dir
from teatree.core.clone_paths import find_clone_path
from teatree.core.models import Ticket, Worktree
from teatree.core.runners.base import RunnerBase, RunnerResult
from teatree.utils import git

if TYPE_CHECKING:
    from teatree.core.models.types import TicketExtra

logger = logging.getLogger(__name__)


class WorktreeProvisioner(RunnerBase):
    """Create the per-repo git worktrees for a STARTED ticket.

    Reads ``ticket.repos`` and ``ticket.extra['branch']`` (set by the CLI at
    scope time) and materialises one ``Worktree`` row + on-disk git worktree
    per repo. Idempotent: re-running over an existing layout is a no-op.
    """

    def __init__(self, ticket: Ticket) -> None:
        self.ticket = ticket

    def run(self) -> RunnerResult:
        ticket = self.ticket
        repos = list(ticket.repos or [])
        if not repos:
            return RunnerResult(ok=False, detail="no repos on ticket")

        extra = cast("TicketExtra", ticket.extra or {})
        branch = extra.get("branch", "")
        if not branch:
            return RunnerResult(ok=False, detail="ticket.extra['branch'] not set — call scope() first")

        workspace = _workspace_dir()
        ticket_dir = workspace / branch
        ticket_dir.mkdir(parents=True, exist_ok=True)

        provisioned: dict[str, str] = dict(extra.get("provision") or {})
        failed: list[str] = []

        for repo_name in repos:
            existing = Worktree.objects.filter(ticket=ticket, repo_path=repo_name).first()
            if existing and (existing.extra or {}).get("worktree_path"):
                provisioned[repo_name] = (existing.extra or {})["worktree_path"]
                continue

            worktree = existing or Worktree.objects.create(
                ticket=ticket,
                repo_path=repo_name,
                branch=branch,
                overlay=ticket.overlay,
            )

            created = self._create(workspace, repo_name, ticket_dir, branch)
            if created is None:
                worktree.delete()
                failed.append(repo_name)
                continue

            wt_path, clone_path = created
            worktree.branch = branch
            worktree.extra = {
                **(worktree.extra or {}),
                "worktree_path": wt_path,
                "clone_path": str(clone_path),
            }
            worktree.save(update_fields=["branch", "extra"])
            provisioned[repo_name] = wt_path

        extra["provision"] = provisioned
        ticket.extra = extra
        ticket.save(update_fields=["extra"])

        if failed:
            return RunnerResult(ok=False, detail=f"failed to create worktrees for: {', '.join(failed)}")
        return RunnerResult(ok=True, detail=f"provisioned {len(provisioned)} worktree(s)")

    @staticmethod
    def _create(workspace: Path, repo_name: str, ticket_dir: Path, branch: str) -> tuple[str, Path] | None:
        """Run ``git worktree add`` for one repo.

        Returns ``(worktree_path, clone_path)`` on success or ``None`` on
        failure (no clone found, or ``git worktree add`` rejected the path).
        Retries without ``-b`` so partial-failure recovery picks up an
        existing branch.
        """
        repo_path = find_clone_path(workspace, repo_name)
        if repo_path is None:
            logger.warning(
                "No git clone found for %s under %s (looked at %s and one-level subdirs)",
                repo_name,
                workspace,
                workspace / repo_name,
            )
            return None

        wt_path = ticket_dir / Path(repo_name).name
        if wt_path.exists():
            return str(wt_path), repo_path

        git.pull_ff_only(str(repo_path))

        ok = git.worktree_add(str(repo_path), str(wt_path), branch, create_branch=True)
        if not ok:
            ok = git.worktree_add(str(repo_path), str(wt_path), branch, create_branch=False)
        if not ok:
            logger.warning("Failed to create worktree for %s at %s", repo_name, wt_path)
            return None

        pv = repo_path / ".python-version"
        pv_dest = wt_path / ".python-version"
        if pv.is_file() and not pv_dest.exists():
            with suppress(OSError):
                pv_dest.symlink_to(pv)

        return str(wt_path), repo_path
