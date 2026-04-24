import logging
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, cast

from teatree.config import workspace_dir as _workspace_dir
from teatree.core.models import Worktree
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

            wt_path = self._create(workspace, repo_name, ticket_dir, branch)
            if wt_path is None:
                # actual failure — drop the row so subsequent runs can retry cleanly
                worktree.delete()
                failed.append(repo_name)
                continue
            if not wt_path:
                # skipped (source repo missing) — keep the row without a path
                continue

            worktree.branch = branch
            worktree.extra = {**(worktree.extra or {}), "worktree_path": wt_path}
            worktree.save(update_fields=["branch", "extra"])
            provisioned[repo_name] = wt_path

        extra["provision"] = provisioned
        ticket.extra = extra
        ticket.save(update_fields=["extra"])

        if failed:
            return RunnerResult(ok=False, detail=f"failed to create worktrees for: {', '.join(failed)}")
        return RunnerResult(ok=True, detail=f"provisioned {len(provisioned)} worktree(s)")

    @staticmethod
    def _create(workspace: Path, repo_name: str, ticket_dir: Path, branch: str) -> str | None:
        """Run ``git worktree add`` for one repo.

        Returns the worktree path on success, ``""`` when the source repo is
        not a git checkout (skipped), or ``None`` on actual failure. Retries
        without ``-b`` so partial-failure recovery picks up an existing branch.
        """
        repo_path = workspace / repo_name
        if not (repo_path / ".git").is_dir():
            logger.info("Skipping %s: not a git repository under %s", repo_name, workspace)
            return ""

        wt_path = ticket_dir / Path(repo_name).name
        if wt_path.exists():
            return str(wt_path)

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

        return str(wt_path)
