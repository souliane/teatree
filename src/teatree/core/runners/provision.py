import logging
import shutil
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

from teatree.config import clone_root, worktree_root
from teatree.core.models import Ticket, Worktree
from teatree.core.public_identity import is_public_github_remote, set_local_noreply_identity
from teatree.core.runners.base import RunnerBase, RunnerResult
from teatree.core.worktree.clone_paths import find_clone_path
from teatree.core.worktree.worktree_paths import paths_match, ticket_dir_for
from teatree.utils import git
from teatree.utils.git_guard import guard_repo_remote_slug, is_github_slug
from teatree.utils.run import CommandFailedError

if TYPE_CHECKING:
    from teatree.core.models.types import TicketExtra

logger = logging.getLogger(__name__)


def _clone_dir_from_worktree(worktree_path: str) -> Path | None:
    """The main clone backing an on-disk worktree, via its shared git dir (#2275).

    ``git rev-parse --git-common-dir`` resolves to the main clone's git directory
    (``<clone>/.git`` for a linked worktree, ``.git`` relative from the clone
    root); its parent is the clone working tree. Returns ``None`` when
    *worktree_path* is not a git worktree, so the adopt path falls back to the
    checkout itself.
    """
    common = git.run(repo=worktree_path, args=["rev-parse", "--git-common-dir"])
    if not common:
        return None
    common_path = Path(common)
    if not common_path.is_absolute():
        common_path = (Path(worktree_path) / common_path).resolve()
    return common_path.parent


@dataclass(frozen=True, slots=True)
class _RegisteredWorktree:
    """One entry from ``git worktree list --porcelain`` — what git believes exists."""

    path: str
    branch: str

    @property
    def on_disk(self) -> bool:
        return Path(self.path).is_dir()


def _registered_worktrees(clone: str) -> list[_RegisteredWorktree]:
    """Every worktree git has REGISTERED for *clone*, including the main checkout.

    Git's registration — not the filesystem — is what refuses a ``git worktree
    add``: a branch is "already checked out" while any registration claims it, even
    one whose directory was deleted. Reading the registrations is therefore the only
    way to see the leftover that blocks provisioning. A detached entry has no
    ``branch`` line and yields an empty ``branch``.
    """
    entries: list[_RegisteredWorktree] = []
    path = ""
    branch = ""
    for line in git.run(repo=clone, args=["worktree", "list", "--porcelain"]).splitlines():
        if line.startswith("worktree "):
            if path:
                entries.append(_RegisteredWorktree(path=path, branch=branch))
            path, branch = line.removeprefix("worktree "), ""
        elif line.startswith("branch refs/heads/"):
            branch = line.removeprefix("branch refs/heads/")
    if path:
        entries.append(_RegisteredWorktree(path=path, branch=branch))
    return entries


def _holds_unsalvageable_work(wt_path: str) -> bool:
    """Whether tearing down *wt_path* would destroy the only copy of some work.

    The #706 data-loss guard, mirroring
    :func:`teatree.core.worktree.reconcile._unpushed_work_for_worktree` and the
    ``recover`` sweeps: a worktree is protected when it holds uncommitted changes,
    or when its HEAD carries commits reachable from NO remote. ``--not --remotes``
    is empty as soon as the tip was pushed anywhere, so a pushed-but-unmerged branch
    is correctly reapable while a genuinely-local tip is not.

    **Fails closed.** An inconclusive probe (``CommandFailedError`` — corrupt repo,
    dangling ref, no commits yet) is treated as "carries work": for a destructive
    decision, "we could not prove this is safe to delete" must protect the worktree,
    never sacrifice it.
    """
    if not Path(wt_path).is_dir():
        return False
    if git.status_porcelain(wt_path).strip():
        return True
    try:
        return bool(git.commits_absent_from_all_remotes(wt_path, "HEAD"))
    except CommandFailedError:
        return True


def _tear_down_worktree(clone: str, wt_path: str, branch: str) -> None:
    """Force-remove a work-free worktree, prune the registration, drop a dangling branch.

    Only ever reached once the checkout has been PROVEN free of unpushed work (see
    :func:`_holds_unsalvageable_work`) or its directory is already gone, so nothing
    recoverable is lost. ``git worktree prune`` is what actually frees the branch —
    a registration whose dir was deleted still makes git refuse the branch as
    "already checked out".

    The branch ref is dropped with ``git branch -d`` (never ``-D``): git's own
    unmerged-branch guard is the unmerged-and-unreferenced check, so a branch still
    carrying commits is KEPT and the caller's recreate simply reuses it via the
    existing no-``-b`` retry. Deleting it when it IS merged is what lets a retry
    branch cleanly off the current default instead of resurrecting a stale tip.
    """
    if Path(wt_path).is_dir():
        git.worktree_remove(clone, wt_path)
    git.run(repo=clone, args=["worktree", "prune"])
    if branch:
        git.check(repo=clone, args=["branch", "-d", branch])


def _reconcile_leftover_worktree(clone: Path, wt_path: Path, branch: str) -> str | None:
    """Make the scope's worktree slot creatable, or ADOPT what is already there (#3234).

    Provisioning must be idempotent. A prior attempt that failed DOWNSTREAM of
    ``git worktree add`` leaves the worktree and/or the branch behind; ``git worktree
    add`` then refuses the path (it exists) AND the branch (it is "already checked
    out"), so provision failed with "failed to create worktrees for: <repo>" and the
    ticket stayed at ``started`` forever — every retry hitting the identical wall.

    Returns the path to ADOPT (provisioning is then a no-op over an existing
    checkout), or ``None`` when the slot is now clear for ``git worktree add``:

    - a healthy registration at the expected path on the CORRECT branch → adopt it;
    - a registration whose directory is GONE → stale git admin; pruned, then recreate;
    - a leftover holding the branch at ANOTHER path, or one sitting at the expected
        path on the WRONG branch → torn down (guarded) and recreated;
    - a leftover carrying work absent from every remote → NEVER destroyed: adopted in
        place when it is on the scope's branch, and otherwise left alone so the caller
        fails loudly rather than silently deleting the only copy of that work.
    """
    clone_str, wt_str = str(clone), str(wt_path)

    # A registration whose dir was deleted still holds its branch hostage. Prune
    # first so the survey below sees only registrations git will really enforce.
    if any(not entry.on_disk for entry in _registered_worktrees(clone_str)):
        git.run(repo=clone_str, args=["worktree", "prune"])

    leftovers = [entry for entry in _registered_worktrees(clone_str) if not paths_match(entry.path, clone_str)]
    at_path = next((entry for entry in leftovers if paths_match(entry.path, wt_str)), None)
    on_branch = next((entry for entry in leftovers if entry.branch == branch), None)

    if at_path is not None and at_path.branch == branch:
        logger.info("Adopting the existing worktree for %s at %s (idempotent re-provision)", branch, wt_str)
        return wt_str

    for leftover in (at_path, on_branch):
        if leftover is None:
            continue
        if _holds_unsalvageable_work(leftover.path):
            if leftover.branch == branch:
                logger.warning(
                    "Leftover worktree for %s at %s carries work that exists on no remote — adopting it "
                    "in place instead of recreating at %s. Push or salvage it to move the worktree.",
                    branch,
                    leftover.path,
                    wt_str,
                )
                return leftover.path
            logger.error(
                "Cannot provision %s at %s: a worktree on branch %s is in the way and carries work that "
                "exists on no remote. Refusing to destroy it — push or salvage that work, then retry.",
                branch,
                wt_str,
                leftover.branch,
            )
            return None
        logger.warning(
            "Cleaning up a broken leftover worktree at %s (branch %s) before provisioning %s at %s",
            leftover.path,
            leftover.branch or "(detached)",
            branch,
            wt_str,
        )
        _tear_down_worktree(clone_str, leftover.path, leftover.branch)

    # A directory git does not know about — a prior ``git worktree add`` that died
    # mid-checkout — still blocks the add. It holds no git history (git has no
    # registration for it), so removing it cannot lose committed work.
    if wt_path.is_dir():
        logger.warning("Removing a partial non-worktree directory left at %s before provisioning", wt_str)
        shutil.rmtree(wt_path, ignore_errors=True)

    return None


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
        # #2275 adopt: repo -> existing on-disk worktree_path. In adopt mode the
        # checkout already exists (the operator ran ``workspace ticket --adopt``
        # from inside it), so it is recorded verbatim and no ``ticket_dir`` under
        # the worktree root is needed. Skip creating that (empty) dir when every
        # repo is adopted so no stray second dir appears.
        adopt = dict(extra.get("adopt") or {})

        ticket_dir = self._existing_ticket_dir(ticket) or ticket_dir_for(worktree_root(), branch)
        if any(repo_name not in adopt for repo_name in repos):
            ticket_dir.mkdir(parents=True, exist_ok=True)

        provisioned: dict[str, str] = dict(extra.get("provision") or {})
        failed: list[str] = []

        for repo_name in repos:
            wt_path = self._provision_repo(
                clone_root_path,
                repo_name,
                ticket_dir,
                branch=branches.get(repo_name, branch),
                adopt_path=adopt.get(repo_name, ""),
            )
            if wt_path is None:
                failed.append(repo_name)
            else:
                provisioned[repo_name] = wt_path

        # #800 N3: canonical locked RMW (was an unlocked extra save).
        ticket.merge_extra(set_keys={"provision": provisioned})

        if failed:
            return RunnerResult(ok=False, detail=f"failed to create worktrees for: {', '.join(failed)}")
        return RunnerResult(ok=True, detail=f"provisioned {len(provisioned)} worktree(s)")

    def _provision_repo(
        self, clones_root: Path, repo_name: str, ticket_dir: Path, *, branch: str, adopt_path: str
    ) -> str | None:
        """Materialise one repo's ``Worktree`` row + checkout; return its path or ``None``.

        Idempotent: a repo whose worktree_path is already recorded is a no-op. In
        adopt mode (*adopt_path* set) the existing checkout is recorded verbatim —
        see :meth:`_create`. On a failed ``git worktree add`` the just-created row
        is rolled back so the ticket carries no half-provisioned repo.
        """
        existing = Worktree.objects.filter(ticket=self.ticket, repo_path=repo_name).first()
        if existing and (existing.extra or {}).get("worktree_path"):
            return (existing.extra or {})["worktree_path"]

        worktree = existing or Worktree.objects.create(
            ticket=self.ticket,
            repo_path=repo_name,
            branch=branch,
            overlay=self.ticket.overlay,
        )

        created = self._create(clones_root, repo_name, ticket_dir, branch, adopt_path=adopt_path)
        if created is None:
            if existing is None:
                worktree.delete()  # roll back only the row we just created, never a reused one
            return None

        wt_path, clone_path = created
        worktree.branch = branch
        worktree.extra = {
            **(worktree.extra or {}),
            "worktree_path": wt_path,
            "clone_path": str(clone_path),
        }
        worktree.save(update_fields=["branch", "extra"])
        return wt_path

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
    def _create(
        clones_root: Path, repo_name: str, ticket_dir: Path, branch: str, *, adopt_path: str = ""
    ) -> tuple[str, Path] | None:
        """Run ``git worktree add`` for one repo, or record an adopted checkout (#2275).

        *clones_root* is the CLONE root (``config.clone_root()``, ``~/workspace``)
        — where source clones are DISCOVERED — NOT the WORKTREE root the new
        worktree lands under (that is *ticket_dir*). Returns
        ``(worktree_path, clone_path)`` on success or ``None`` on failure (no clone
        found, or ``git worktree add`` rejected the path). Retries without ``-b`` so
        partial-failure recovery picks up an existing branch.

        *adopt_path* (#2275): when set, the branch's worktree already exists on
        disk (the operator ran ``workspace ticket --adopt`` from inside it), so its
        path is recorded verbatim — never ``git worktree add`` (git would refuse
        the already-checked-out branch and it would create a second dir). The
        backing clone is the discovered clone, or the checkout's own shared git dir
        when it lives outside *clones_root*.
        """
        repo_path = find_clone_path(clones_root, repo_name)
        if adopt_path:
            clone_path = repo_path or _clone_dir_from_worktree(adopt_path)
            return adopt_path, clone_path or Path(adopt_path)
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

        # #3234: reconcile whatever a prior failed attempt left behind BEFORE adding.
        # A leftover worktree/branch makes ``git worktree add`` refuse both the path
        # and the branch, which stranded the ticket at ``started`` forever.
        adopted = _reconcile_leftover_worktree(repo_path, wt_path, branch)
        if adopted is not None:
            return adopted, repo_path

        git.pull_ff_only(str(repo_path))

        ok = git.worktree_add(str(repo_path), str(wt_path), branch, create_branch=True)
        if not ok:
            ok = git.worktree_add(str(repo_path), str(wt_path), branch, create_branch=False)
        if not ok:
            logger.warning("Failed to create worktree for %s at %s", repo_name, wt_path)
            return None

        try:
            WorktreeProvisioner._finalize(repo_path, wt_path)
        except Exception:
            # #3234: a step that fails AFTER the worktree exists must not strand it —
            # the leftover is exactly what refuses the next ``git worktree add``. The
            # checkout was created moments ago and carries no work, so tearing it down
            # is free and leaves the retry a clean slate. Fail the provision loudly:
            # a half-provisioned worktree is not a usable one.
            logger.exception(
                "Provision step failed after creating the worktree for %s at %s — tearing it down so the "
                "retry starts clean (#3234).",
                repo_name,
                wt_path,
            )
            _tear_down_worktree(str(repo_path), str(wt_path), branch)
            return None

        return str(wt_path), repo_path

    @staticmethod
    def _finalize(repo_path: Path, wt_path: Path) -> None:
        """The post-``worktree add`` steps. Raising here tears the new worktree back down.

        Kept separate from :meth:`_create` so every step that runs AFTER the checkout
        exists sits behind one rollback boundary — a new step cannot be added without
        inheriting the teardown-on-failure contract (#3234).
        """
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
        #
        # #3234: a failure here now ROLLS THE WORKTREE BACK rather than logging and
        # carrying on. The old fail-open left a worktree with the inherited identity
        # in place (the #755 lesson said "surface it loudly"), but a surfaced warning
        # on a worktree that still gets committed from is the same leak by a slower
        # route — and the stranded checkout then blocked its own re-provision.
        if is_public_github_remote(git.remote_url(str(repo_path))):
            set_local_noreply_identity(str(wt_path))
