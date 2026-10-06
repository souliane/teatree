import logging
import shutil
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

from teatree.config import clone_root, cold_reader, get_effective_settings, worktree_root
from teatree.core.gates.single_branch_repo_guard import (
    GATE_KEY,
    check_branch_admitted,
    deny_reason,
    resolve_pinned_branch,
)
from teatree.core.models import Ticket, Worktree
from teatree.core.overlay_loader import get_overlay_for_ticket
from teatree.core.public_identity import is_public_github_remote, set_local_noreply_identity
from teatree.core.runners.base import RunnerBase, RunnerResult
from teatree.core.worktree.checkout_disposal import disposal_refusal
from teatree.core.worktree.checkout_liveness import wrong_venue_reason
from teatree.core.worktree.clone_paths import find_clone_path, git_common_clone_dir
from teatree.core.worktree.clone_provision import ensure_clone
from teatree.core.worktree.target_branch import resolve_target_branch
from teatree.core.worktree.ticket_workspace import (
    TicketWorkspaceDivergenceError,
    assert_joins_ticket_workspace,
    ticket_workspace_dir_or_refuse,
)
from teatree.core.worktree.venue_safe_registry import WorkPresence, prune_worktrees, unsalvageable_work_state
from teatree.core.worktree.worktree_paths import paths_match, ticket_dir_for
from teatree.core.worktree.worktree_roots import CheckoutState, probe_checkout
from teatree.utils import git
from teatree.utils.git_guard import guard_repo_remote_slug, is_remote_project_path
from teatree.utils.git_run import git_env_without_overrides, run_with_status

if TYPE_CHECKING:
    from teatree.core.models.types import TicketExtra

logger = logging.getLogger(__name__)


def _clone_dir_from_worktree(worktree_path: str) -> Path | None:
    """The main clone backing an on-disk worktree, via its shared git dir (#2275).

    Delegates to the shared :func:`~teatree.core.worktree.clone_paths.git_common_clone_dir`
    so this seam and clone RESOLUTION cannot drift on what "the clone behind this
    worktree" means. Returns ``None`` when *worktree_path* is not a git worktree,
    so the adopt path falls back to the checkout itself.
    """
    return git_common_clone_dir(worktree_path)


def _recorded_checkout_is_live(recorded: str, *, clone: Path | None) -> bool:
    """Is the checkout a row records still THERE? Positive proof only.

    The provisioning half of the liveness question the reaper asks in
    :mod:`teatree.core.worktree.checkout_liveness`, and the answer it needs is the
    opposite one. A reaper deletes, so it may act only on proof of DEATH; the
    preflight refuses to re-materialise a checkout on this answer, so it may act
    only on proof of LIFE. Anything less falls through and re-provisions, which
    adds a checkout and removes nothing.

    *clone* is the source clone as THIS context reaches it: a checkout records its
    admin dir as an absolute path written by whatever context created it, so git
    answers "not a git repository" here for a checkout that is alive elsewhere. The
    clone's own admin entry, looked up by name, proves that checkout live, and the
    preflight then refuses it rather than re-provisioning over it.
    """
    return probe_checkout(Path(recorded), clone=clone) is CheckoutState.CHECKOUT


def _unreadable_checkout_reason(checkout: Path) -> str:
    """Why a checkout git cannot read HERE is refused — never re-created, never removed."""
    cause = wrong_venue_reason(checkout)
    if not cause:
        probe = run_with_status(repo=str(checkout), args=["rev-parse", "--git-dir"], env=git_env_without_overrides())
        cause = f"{checkout} is a live checkout git cannot read in this execution context ({probe.stderr.strip()})"
    return f"{cause}; re-provision or remove it from the context that owns it"


@dataclass(frozen=True, slots=True)
class _LiveCheckout:
    """A recorded checkout git resolves here, reused as it stands with no network read."""

    repo_name: str
    path: str


@dataclass(frozen=True, slots=True)
class _RepoPlan:
    """A repo the preflight cleared: cut at *start_point*, or record the checkout at *adopt_path*."""

    repo_name: str
    branch: str
    existing: Worktree | None
    clone: Path
    start_point: str = ""
    adopt_path: str = ""


@dataclass(frozen=True, slots=True)
class _Refusal:
    reason: str
    retryable: bool = False


def _refused(refusals: list[_Refusal]) -> RunnerResult:
    """The whole ticket refused before anything was cut; retryable only when every refusal is."""
    for refusal in refusals:
        logger.error("%s", refusal.reason)
    return RunnerResult(
        ok=False,
        detail="refused before cutting any worktree — " + "; ".join(refusal.reason for refusal in refusals),
        retryable=all(refusal.retryable for refusal in refusals),
    )


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


def _tear_down_worktree(clone: str, wt_path: str, branch: str) -> None:
    """Force-remove a work-free worktree, prune the registration, drop a dangling branch.

    Only ever reached once the checkout has been PROVEN free of unpushed work (see
    :func:`~teatree.core.worktree.venue_safe_registry.unsalvageable_work_state`) or
    its directory is provably gone, so nothing recoverable is lost. The prune is what
    actually frees the branch — a registration whose dir was deleted still makes git
    refuse the branch as "already checked out" — and it is venue-gated (#4287), so a
    clone holding a registration this context cannot vouch for keeps its branch held
    rather than stranding somebody else's checkout.

    The branch ref is dropped with ``git branch -d`` (never ``-D``): git's own
    unmerged-branch guard is the unmerged-and-unreferenced check, so a branch still
    carrying commits is KEPT and the caller's recreate simply reuses it via the
    existing no-``-b`` retry. Deleting it when it IS merged is what lets a retry
    branch cleanly off the current default instead of resurrecting a stale tip.
    """
    if Path(wt_path).is_dir():
        git.worktree_remove(clone, wt_path)
    prune_worktrees(clone)
    if branch:
        git.check(repo=clone, args=["branch", "-d", branch])


def _refuse_unclearable_leftover(
    leftover: _RegisteredWorktree, work: WorkPresence, *, branch: str, wt_str: str
) -> None:
    """Refuse the provision over a leftover that cannot be cleared — never destroy it, never adopt it.

    Reached only for a leftover OUTSIDE the scope's slot whose teardown could cost work:
    one carrying commits that exist on no remote, or one this context cannot READ (#4287).
    A checkout elsewhere is not one this ticket owns, even on its branch — claiming it is
    ``workspace ticket --adopt``'s job, run from inside it.
    """
    if work is WorkPresence.UNKNOWN:
        logger.error(
            "Cannot provision %s at %s: the leftover worktree at %s is unreadable in this execution "
            "context, so whether it holds unpushed work is unknowable here. Refusing to touch it — act "
            "from the context that resolves it.",
            branch,
            wt_str,
            leftover.path,
        )
    elif leftover.branch == branch:
        logger.error(
            "Cannot provision %s at %s: the branch is checked out at %s, outside this ticket's slot, and "
            "carries work that exists on no remote. Refusing to adopt a checkout this ticket does not own "
            "or to destroy it — push or salvage that work, or run `workspace ticket --adopt` from inside it.",
            branch,
            wt_str,
            leftover.path,
        )
    else:
        logger.error(
            "Cannot provision %s at %s: a worktree on branch %s is in the way and carries work that exists on "
            "no remote. Refusing to destroy it — push or salvage that work, then retry.",
            branch,
            wt_str,
            leftover.branch,
        )


def _reconcile_leftover_worktree(clone: Path, wt_path: Path, branch: str, *, ticket_id: int | None) -> str | None:
    """Make the scope's worktree slot creatable, or ADOPT what is already there (#3234).

    Provisioning must be idempotent. A prior attempt that failed DOWNSTREAM of
    ``git worktree add`` leaves the worktree and/or the branch behind; ``git worktree
    add`` then refuses the path (it exists) AND the branch (it is "already checked
    out"), so provision failed with "failed to create worktrees for: <repo>" and the
    ticket stayed at ``work_started`` forever — every retry hitting the identical wall.

    Three outcomes: the path to ADOPT (provisioning is then a no-op over an existing
    checkout), ``""`` when the slot is now clear for ``git worktree add``, or ``None``
    when the slot cannot be cleared and provisioning must not proceed.

    - a healthy registration at the expected path on the CORRECT branch → adopt it;
    - a registration whose directory is GONE → stale git admin; pruned, then recreate;
    - a leftover holding the branch at ANOTHER path, or one sitting at the expected
        path on the WRONG branch → torn down (guarded) and recreated;
    - a leftover outside the slot carrying work absent from every remote → refused:
        never destroyed, and never adopted even on the scope's branch — a checkout
        elsewhere is not one this ticket owns;
    - a leftover this context cannot READ — so whether it carries work is unknowable
        here — → refused, never adopted and never torn down (#4287);
    - anything standing in the slot that this context cannot prove disposable →
        refused (#3967).

    Every step that would REMOVE something asks
    :func:`~teatree.core.worktree.checkout_disposal.disposal_refusal` first. The
    registration survey below is scoped to *clone* as THIS context reaches it, so "git
    does not know about it" is a statement about the vantage point, not about the disk.
    """
    clone_str, wt_str = str(clone), str(wt_path)

    # A registration whose dir was deleted still holds its branch hostage. Prune
    # first so the survey below sees only registrations git will really enforce.
    if any(not entry.on_disk for entry in _registered_worktrees(clone_str)):
        prune_worktrees(clone_str)

    leftovers = [entry for entry in _registered_worktrees(clone_str) if not paths_match(entry.path, clone_str)]
    at_path = next((entry for entry in leftovers if paths_match(entry.path, wt_str)), None)
    on_branch = next((entry for entry in leftovers if entry.branch == branch), None)

    if at_path is not None and at_path.branch == branch:
        if probe_checkout(wt_path) is not CheckoutState.CHECKOUT:
            logger.error("Cannot provision %s at %s: %s", branch, wt_str, _unreadable_checkout_reason(wt_path))
            return None
        logger.info("Adopting the existing worktree for %s at %s (idempotent re-provision)", branch, wt_str)
        return wt_str

    for leftover in (at_path, on_branch):
        if leftover is None:
            continue
        work = unsalvageable_work_state(leftover.path)
        if work is not WorkPresence.NONE:
            _refuse_unclearable_leftover(leftover, work, branch=branch, wt_str=wt_str)
            return None
        logger.warning(
            "Cleaning up a broken leftover worktree at %s (branch %s) before provisioning %s at %s",
            leftover.path,
            leftover.branch or "(detached)",
            branch,
            wt_str,
        )
        if refusal := disposal_refusal(Path(leftover.path), clone=clone, requesting_ticket_id=ticket_id):
            logger.error("Cannot provision %s at %s: %s", branch, wt_str, refusal)
            return None
        _tear_down_worktree(clone_str, leftover.path, leftover.branch)

    # A directory that never claimed to be a checkout — a prior ``git worktree add``
    # that died before writing its ``.git`` entry — still blocks the add, and is the
    # one thing this context can prove holds no history of its own.
    if wt_path.is_dir():
        if refusal := disposal_refusal(wt_path, clone=clone, requesting_ticket_id=ticket_id):
            logger.error("Cannot provision %s at %s: %s", branch, wt_str, refusal)
            return None
        logger.warning("Removing a partial non-worktree directory left at %s before provisioning", wt_str)
        shutil.rmtree(wt_path, ignore_errors=True)

    return ""


class WorktreeProvisioner(RunnerBase):
    """Create the per-repo git worktrees for a WORK_STARTED ticket.

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

        # The ROOT comes from the TICKET's overlay: a container run resolves no ambient
        # overlay, drops the segment, and splits the ticket across two roots. A legacy
        # blank-overlay ticket has nothing to name, so ``or None`` keeps it ambient.
        try:
            ticket_dir = self._existing_ticket_dir(ticket) or ticket_dir_for(
                worktree_root(overlay=ticket.overlay or None), branch
            )
        except TicketWorkspaceDivergenceError as exc:
            logger.exception("Ticket workspace is already split")
            return RunnerResult(ok=False, detail=str(exc))
        preflight = [
            self._preflight(clone_root_path, repo_name, branches.get(repo_name, branch), adopt.get(repo_name, ""))
            for repo_name in repos
        ]
        if refusals := [outcome for outcome in preflight if isinstance(outcome, _Refusal)]:
            return _refused(refusals)

        if any(repo_name not in adopt for repo_name in repos):
            ticket_dir.mkdir(parents=True, exist_ok=True)
        plans = [outcome for outcome in preflight if not isinstance(outcome, _Refusal)]
        return self._cut(plans, ticket_dir, provisioned=dict(extra.get("provision") or {}))

    def _cut(
        self, plans: list[_RepoPlan | _LiveCheckout], ticket_dir: Path, *, provisioned: dict[str, str]
    ) -> RunnerResult:
        """Phase two: materialise every preflighted repo; a failure here fails only that repo."""
        failed: list[str] = []
        divergence_detail = ""

        for plan in plans:
            try:
                wt_path = self._provision_repo(plan, ticket_dir)
            except TicketWorkspaceDivergenceError as exc:
                logger.exception("Candidate worktree would split the ticket workspace")
                divergence_detail = str(exc)
                break
            if wt_path is None:
                failed.append(plan.repo_name)
            else:
                provisioned[plan.repo_name] = wt_path

        # #800 N3: canonical locked RMW (was an unlocked extra save).
        self.ticket.merge_extra(set_keys={"provision": provisioned})

        if divergence_detail:
            return RunnerResult(ok=False, detail=divergence_detail)
        if failed:
            return RunnerResult(ok=False, detail=f"failed to create worktrees for: {', '.join(failed)}")
        return RunnerResult(ok=True, detail=f"provisioned {len(provisioned)} worktree(s)")

    def _preflight(
        self, clones_root: Path, repo_name: str, branch: str, adopt_path: str
    ) -> _RepoPlan | _LiveCheckout | _Refusal:
        """Every check that can refuse *repo_name*, run before any directory, row or worktree exists.

        A checkout only the clone vouches for is refused: every git command run inside it fails.
        """
        existing = Worktree.objects.filter(ticket=self.ticket, repo_path=repo_name).first()
        recorded = existing.worktree_path if existing else ""
        if recorded:
            if probe_checkout(Path(recorded)) is CheckoutState.CHECKOUT:
                return _LiveCheckout(repo_name, recorded)
            if _recorded_checkout_is_live(recorded, clone=find_clone_path(clones_root, repo_name)):
                return _Refusal(f"{repo_name}: {_unreadable_checkout_reason(Path(recorded))}")

        if refusal := self._single_branch_refusal(repo_name, branch):
            return _Refusal(refusal)

        if adopt_path:
            clone = find_clone_path(clones_root, repo_name) or _clone_dir_from_worktree(adopt_path)
            return _RepoPlan(repo_name, branch, existing, clone=clone or Path(adopt_path), adopt_path=adopt_path)
        return self._plan_cut(clones_root, repo_name, branch, existing)

    def _plan_cut(
        self, clones_root: Path, repo_name: str, branch: str, existing: Worktree | None
    ) -> _RepoPlan | _Refusal:
        """The clone and the freshly fetched start point a new worktree for *repo_name* is cut from."""
        clone = ensure_clone(clones_root, repo_name, get_overlay_for_ticket(self.ticket))
        if clone is None:
            return _Refusal(
                f"No git clone found or creatable for {repo_name} under {clones_root} "
                f"(looked at {clones_root / repo_name} and one-level subdirs)"
            )
        # #2276: ``find_clone_path`` resolves by basename, so a sibling clone of the same name could be cut.
        if is_remote_project_path(repo_name):
            guard_repo_remote_slug(str(clone), repo_name)
        try:
            start_point = git.cut_start_point(
                str(clone), branch, base=resolve_target_branch(self.ticket, str(clone), branch=branch)
            )
        except git.NoStartPointError as exc:
            return _Refusal(f"{repo_name}: {exc}")
        except git.RemoteReadError as exc:
            return _Refusal(f"{repo_name}: cannot refresh from its remote: {exc}", retryable=True)
        return _RepoPlan(repo_name, branch, existing, clone=clone, start_point=start_point)

    def _provision_repo(self, plan: _RepoPlan | _LiveCheckout, ticket_dir: Path) -> str | None:
        """Materialise one preflighted repo's ``Worktree`` row + checkout; return its path or ``None``.

        A live recorded checkout is a no-op. On a failed ``git worktree add`` — or a
        RAISED refusal — the just-created row is rolled back so the ticket carries no
        half-provisioned repo.
        """
        if isinstance(plan, _LiveCheckout):
            return plan.path

        # Most rows are registered HERE rather than at the ad-hoc adopt seam, and
        # ``adopt_path`` records a checkout verbatim — wherever the operator ran from.
        slot = Path(plan.adopt_path) if plan.adopt_path else ticket_dir / Path(plan.repo_name).name
        assert_joins_ticket_workspace(self.ticket, slot)

        worktree = plan.existing or Worktree.objects.create(
            ticket=self.ticket,
            repo_path=plan.repo_name,
            branch=plan.branch,
            overlay=self.ticket.overlay,
        )

        try:
            created = self._create(plan, slot)
        except Exception:
            # A RAISED failure is a failed provision exactly as a ``None`` return is,
            # so it must roll the row back too — never strand a row for an unprovisioned repo.
            if plan.existing is None:
                worktree.delete()
            raise
        if created is None:
            if plan.existing is None:
                worktree.delete()  # roll back only the row we just created, never a reused one
            return None

        wt_path, clone_path = created
        worktree.branch = plan.branch
        worktree.extra = {
            **(worktree.extra or {}),
            "worktree_path": wt_path,
            "clone_path": str(clone_path),
        }
        worktree.save(update_fields=["branch", "extra"])
        return wt_path

    def _single_branch_refusal(self, repo_name: str, branch: str) -> str:
        """The deny text when *repo_name* is single-branch and *branch* is not its pinned one.

        Checked BEFORE the ``Worktree`` row is created, so a refusal leaves no
        half-provisioned state to roll back. Empty string when the repo is not
        declared single-branch or the branch IS the pinned one — the overwhelming
        majority of provisions, which pay one config read.
        """
        if not cold_reader.bool_setting(GATE_KEY, default=True):
            return ""
        settings = get_effective_settings(self.ticket.overlay or None)
        pinned = resolve_pinned_branch(repo_name, list(settings.single_branch_repos))
        finding = check_branch_admitted(branch, pinned_branch=pinned)
        if finding is None:
            return ""
        return deny_reason(finding, pinned_branch=pinned, repo=repo_name)

    @staticmethod
    def _existing_ticket_dir(ticket: Ticket) -> Path | None:
        """The shared parent dir of this ticket's already-materialised worktrees.

        Delegates to
        :func:`~teatree.core.worktree.ticket_workspace.ticket_workspace_dir_or_refuse`
        so the provisioner's co-location rule and the refusal the ad-hoc registration
        seams enforce are the SAME predicate. They were the same rule expressed twice,
        which is how the ad-hoc seams came to have no rule at all: a repo added later
        co-locates as a sibling in the ticket's existing dir, ``None`` (nothing
        materialised yet) leaves the caller's root-derived default in force, and an
        existing SPLIT refuses rather than picking a side.
        """
        return ticket_workspace_dir_or_refuse(ticket)

    def _create(self, plan: _RepoPlan, wt_path: Path) -> tuple[str, Path] | None:
        """Run ``git worktree add`` for one preflighted repo, or record an adopted checkout (#2275).

        Returns ``(worktree_path, clone_path)`` on success or ``None`` on failure (a
        slot this context cannot clear, or ``git worktree add`` rejected the path).

        An adopted checkout already exists on disk (the operator ran ``workspace
        ticket --adopt`` from inside it), so its path is recorded verbatim — never
        ``git worktree add``, which would refuse the checked-out branch and create a
        second dir.
        """
        if plan.adopt_path:
            return plan.adopt_path, plan.clone

        # #3234: reconcile whatever a prior failed attempt left behind BEFORE adding.
        # A leftover worktree/branch makes ``git worktree add`` refuse both the path
        # and the branch, which stranded the ticket at ``work_started`` forever.
        slot = _reconcile_leftover_worktree(plan.clone, wt_path, plan.branch, ticket_id=self.ticket.pk)
        if slot is None:
            return None
        if slot:
            return slot, plan.clone

        return self._materialise(plan, wt_path)

    @staticmethod
    def _materialise(plan: _RepoPlan, wt_path: Path) -> tuple[str, Path] | None:
        """``git worktree add`` into a CLEARED slot, rolled back if a later step fails.

        A new branch starts at the preflight's freshly fetched start point; an existing
        local branch is checked out as it stands. Every step that runs after the
        checkout exists sits behind the one rollback boundary here, so a new step
        cannot be added without inheriting it (#3234).
        """
        repo, branch = str(plan.clone), plan.branch
        ok = git.worktree_add(repo, str(wt_path), branch, start_point=plan.start_point)
        if not ok:
            ok = git.worktree_add(repo, str(wt_path), branch, create_branch=False)
        if not ok:
            logger.warning("Failed to create worktree for %s at %s", plan.repo_name, wt_path)
            return None

        try:
            WorktreeProvisioner._finalize(plan.clone, wt_path)
        except Exception:
            # #3234: a step that fails AFTER the worktree exists must not strand it —
            # the leftover is exactly what refuses the next ``git worktree add``. The
            # checkout was created moments ago and carries no work, so tearing it down
            # is free and leaves the retry a clean slate. Fail the provision loudly:
            # a half-provisioned worktree is not a usable one.
            logger.exception(
                "Provision step failed after creating the worktree for %s at %s — tearing it down so the "
                "retry starts clean (#3234).",
                plan.repo_name,
                wt_path,
            )
            _tear_down_worktree(repo, str(wt_path), branch)
            return None

        return str(wt_path), plan.clone

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
