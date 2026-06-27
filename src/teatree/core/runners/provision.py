import logging
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, cast

from teatree.config import clone_root, worktree_root
from teatree.core.clone_paths import find_clone_path
from teatree.core.models import Ticket, Worktree
from teatree.core.public_identity import is_public_github_remote, set_local_noreply_identity
from teatree.core.runners.base import RunnerBase, RunnerResult
from teatree.utils import git
from teatree.utils.git_guard import guard_repo_remote_slug, is_github_slug

if TYPE_CHECKING:
    from teatree.core.models.types import TicketExtra

logger = logging.getLogger(__name__)


class WorktreeProvisioner(RunnerBase):
    """Create the per-repo git worktrees for a STARTED ticket.

    Reads ``ticket.repos`` and ``ticket.extra['branch']`` (set by the CLI at
    scope time) and materialises one ``Worktree`` row + on-disk git worktree
    per repo. Idempotent: re-running over an existing layout is a no-op.

    #33: a ticket whose repos live on DIFFERENT branches maps each repo to its
    own branch in ``ticket.extra['branches']`` (repo → branch); a repo absent
    from the map falls back to ``extra['branch']``. Every repo provisions as a
    SIBLING in one dir even when the repos are on split per-repo branches — this
    is what lets an e2e / workspace-ticket stack compose split branches together.

    The ticket dir is normally ``<workspace>/<extra['branch']>``, but when the
    ticket ALREADY has materialised worktrees (a repo added to an in-flight
    ticket via ``workspace ticket --repos``) the dir is taken from the existing
    worktrees' shared parent so the added repo co-locates as a sibling — see
    ``_existing_ticket_dir``. This keeps an added FE next to the backend even
    when ``extra['branch']`` has drifted from the original dir name (the
    ``auto:<branch>`` ticket case).
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

        # #33: a ticket whose repos live on DIFFERENT branches maps each one
        # in ``extra['branches']``. The ticket DIR is always ``branch`` so all
        # repos provision as SIBLINGS in one dir; only the per-repo git branch
        # differs. Repos absent from the map fall back to ``branch``.
        branches = dict(extra.get("branches") or {})

        # Two DISTINCT roots (the #regroup split): worktrees are CREATED under the
        # per-overlay WORKTREE root, but their source clones are DISCOVERED under
        # the CLONE root (``~/workspace``). Passing the worktree root to
        # ``find_clone_path`` would scan the wrong dir and fail "No git clone found".
        clone_root_path = clone_root()
        # A repo ADDED to a ticket that already has materialised worktrees must
        # co-locate as a SIBLING of the existing ones — derive the ticket dir
        # from an existing worktree's parent, not blindly from ``branch``. The
        # ``auto:<branch>`` case is where this matters: the first worktree lives
        # in ``<worktree_root>/<actual-branch>`` while ``extra['branch']`` may have
        # been (re)set to a pk-default like ``<pk>-ticket`` by a later scope(),
        # so ``worktree_root / branch`` would split the second repo into a new dir.
        ticket_dir = self._existing_ticket_dir(ticket) or (worktree_root() / branch)
        ticket_dir.mkdir(parents=True, exist_ok=True)

        provisioned: dict[str, str] = dict(extra.get("provision") or {})
        failed: list[str] = []

        for repo_name in repos:
            existing = Worktree.objects.filter(ticket=ticket, repo_path=repo_name).first()
            if existing and (existing.extra or {}).get("worktree_path"):
                provisioned[repo_name] = (existing.extra or {})["worktree_path"]
                continue

            repo_branch = branches.get(repo_name, branch)

            worktree = existing or Worktree.objects.create(
                ticket=ticket,
                repo_path=repo_name,
                branch=repo_branch,
                overlay=ticket.overlay,
            )

            created = self._create(clone_root_path, repo_name, ticket_dir, repo_branch)
            if created is None:
                worktree.delete()
                failed.append(repo_name)
                continue

            wt_path, clone_path = created
            worktree.branch = repo_branch
            worktree.extra = {
                **(worktree.extra or {}),
                "worktree_path": wt_path,
                "clone_path": str(clone_path),
            }
            worktree.save(update_fields=["branch", "extra"])
            provisioned[repo_name] = wt_path

        # #800 N3: canonical locked RMW (was an unlocked extra save).
        ticket.merge_extra(set_keys={"provision": provisioned})

        if failed:
            return RunnerResult(ok=False, detail=f"failed to create worktrees for: {', '.join(failed)}")
        return RunnerResult(ok=True, detail=f"provisioned {len(provisioned)} worktree(s)")

    @staticmethod
    def _existing_ticket_dir(ticket: Ticket) -> Path | None:
        """The shared parent dir of this ticket's already-materialised worktrees.

        Returns the common parent of every existing ``Worktree`` whose
        ``worktree_path`` is on disk, so a repo added later co-locates as a
        sibling there rather than in a fresh ``<workspace>/<branch>`` dir. A
        repo worktree lives at ``<ticket_dir>/<repo-basename>``, so its parent
        IS the ticket dir. Returns ``None`` when the ticket has no materialised
        worktree yet (first provision) or when the existing ones disagree on a
        parent (a pre-existing split we don't paper over), leaving the caller's
        ``workspace / branch`` default in force.
        """
        parents = {
            Path(path).parent
            for wt in Worktree.objects.filter(ticket=ticket)
            if (path := (wt.extra or {}).get("worktree_path")) and Path(path).is_dir()
        }
        return parents.pop() if len(parents) == 1 else None

    @staticmethod
    def _create(clones_root: Path, repo_name: str, ticket_dir: Path, branch: str) -> tuple[str, Path] | None:
        """Run ``git worktree add`` for one repo.

        *clones_root* is the CLONE root (``config.clone_root()``, ``~/workspace``)
        — where source clones are DISCOVERED — NOT the WORKTREE root the new
        worktree lands under (that is *ticket_dir*). Returns
        ``(worktree_path, clone_path)`` on success or ``None`` on failure (no clone
        found, or ``git worktree add`` rejected the path). Retries without ``-b`` so
        partial-failure recovery picks up an existing branch.
        """
        repo_path = find_clone_path(clones_root, repo_name)
        if repo_path is None:
            logger.warning(
                "No git clone found for %s under %s (looked at %s and one-level subdirs)",
                repo_name,
                clones_root,
                clones_root / repo_name,
            )
            return None

        # #2276: ``find_clone_path`` resolves by basename, so a SIBLING clone
        # of the same name (a different ``origin``) would be cut silently. When
        # ``repo_name`` is an ``owner/repo`` slug it carries a canonical remote
        # identity to enforce — refuse loudly if the resolved clone's ``origin``
        # is a different repo, before ``git worktree add``. A bare basename has
        # no slug to compare against, so the guard is skipped (it must never
        # crash the legitimate ``--repos <basename>`` flow).
        if is_github_slug(repo_name):
            guard_repo_remote_slug(str(repo_path), repo_name)

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

        # #762: a worktree off a PUBLIC souliane/* clone gets the
        # configured noreply git identity set clone-local, so every
        # commit path uses it instead of the inherited identity. Scoped
        # by remote — non-github / private clones are left as-is.
        # #2655: pass the full remote URL (host intact), NOT the
        # host-stripped slug — ``is_public_github_remote`` must see the
        # host to refuse a non-github (e.g. gitlab) remote whose bare
        # ``owner/repo`` would otherwise be resolved against github.com.
        if is_public_github_remote(git.remote_url(str(repo_path))):
            try:
                set_local_noreply_identity(str(wt_path))
            except Exception:
                # Do NOT silently swallow — if this fails the worktree
                # keeps the inherited identity and the condition recurs
                # invisibly (the #755 fail-open lesson). Surface it
                # loudly; the worktree is still usable but this needs
                # action, not a soft warning.
                logger.exception(
                    "Failed to set the configured noreply git identity on public "
                    "souliane worktree %s — commits here may use the inherited "
                    "identity (#762). Set the clone-local git identity before "
                    "committing.",
                    wt_path,
                )

        return str(wt_path), repo_path
