"""Done-detection + analyze-before-wipe, the one consolidated worktree reaping pass.

The redesign's core. Tearing a worktree down is destructive (git worktree +
branch removal, the per-worktree Postgres DB, docker containers/images AND
volumes), so the bar is two independent gates, both of which must pass:

1. ``worktree_is_done`` — the NECESSARY gate. A worktree is done only when its
ticket reached a genuinely-terminal state (``MERGED`` / ``DELIVERED`` /
``IGNORED`` — ``SHIPPED`` is excluded: a PR is still open, the work is
unfinished) OR the forge reports the branch squash-merged. It reads the FSM
state first, so it SURVIVES a deleted local branch ref — the rc=128 probe
failure that left ~76 merged worktrees stranded when teardown relied on git alone.

2. ``analyze_worktree_changes`` — the SUFFICIENT gate, and the PRIMARY safety
(the #706 data-loss guard hoisted to an explicit, named step). Even on a done
ticket, EVERY unpushed commit AND every uncommitted change must be PROVEN
redundant — content-equivalent on a remote / ``origin/main`` by **patch-id**
(not subject) on the CURRENT tip, or the tip's tree equals the squash/merge
commit's tree. A merged-PR signal alone is NOT proof — post-merge commits are
kept. Any change NOT proven redundant marks the worktree potentially-needed: it
is KEPT and reported, never wiped (salvage — push-to-PR via ``t3 pr create`` — is
a separate action). The analysis fails CLOSED: an inconclusive git probe keeps it.

:func:`reap_done_worktree` (one row) and :func:`reap_done_worktrees` (a workspace
sweep) are the single consolidated pass that replaces the three former clean-all
passes (``reap_squash_merged_worktrees``, the ``CREATED``-row loop,
``clean_merged_worktrees``). The same per-worktree logic backs the FSM-automatic
teardown (``WorktreeTeardown`` on the merge transition), so the loop tears a
ticket's worktrees down the moment it reaches done — ``clean-all`` is the
exception net that catches whatever slipped through.
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path

from teatree.config import load_config
from teatree.core.branch_classification import (
    _branch_tree_matches_squash,
    content_equivalence_blockers,
    is_squash_merged,
)
from teatree.core.clean_ignore import is_clean_ignored
from teatree.core.cleanup import _effective_target, _EffectiveTarget, _resolve_worktree_path, cleanup_worktree
from teatree.core.cleanup_orphan_ref import classify_orphan_ref
from teatree.core.clone_paths import resolve_clone_path
from teatree.core.models import Ticket, Worktree
from teatree.core.worktree_env import CACHE_DIRNAME, CACHE_FILENAME
from teatree.utils import git
from teatree.utils.run import CommandFailedError

logger = logging.getLogger(__name__)

# Terminal ticket states that authorise teardown. SHIPPED is excluded on purpose
# — a shipped ticket still has an OPEN PR, so the work is not finished.
_DONE_TICKET_STATES = frozenset(
    {Ticket.State.MERGED, Ticket.State.DELIVERED, Ticket.State.IGNORED},
)

# Regenerable artifacts a "real uncommitted change" probe must ignore: provisioning
# writes the env cache into every worktree, so a porcelain status listing only
# those is still clean for the wipe decision.
_REGENERABLE_WORKTREE_PATHS = (CACHE_FILENAME, f"{CACHE_DIRNAME}/")
_PORCELAIN_STATUS_PREFIX_WIDTH = 3
_PREVIEW_LIMIT = 3


@dataclass(frozen=True, slots=True)
class DoneSignal:
    """Whether a worktree is teardown-eligible, and the signal that decided it.

    ``source`` names the decision for the ``clean-all --dry-run`` report and the
    reaper's result line: ``ticket-state:<state>`` for the FSM path,
    ``squash-merged`` for the forge path, ``not-done:<state>`` when kept.
    """

    done: bool
    source: str


@dataclass(frozen=True, slots=True)
class ChangeAnalysis:
    """The per-change redundancy verdict for one worktree.

    ``proven_redundant`` is ``True`` only when EVERY uncommitted change and
    unpushed commit is provably already upstream. ``kept_reasons`` is non-empty
    iff the worktree is potentially-needed — each entry names a change that could
    not be proven redundant, so the caller reports exactly why it was kept.
    """

    proven_redundant: bool
    kept_reasons: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ReapOutcome:
    """The disposition of one worktree under :func:`reap_done_worktree`.

    ``action`` is ``wiped`` / ``kept`` / ``would-wipe`` (dry-run) / ``skipped``
    (clean_ignore). ``label`` is the human-readable result line. ``errors`` carries
    any non-fatal teardown-step failure surfaced by :func:`cleanup_worktree`.
    """

    action: str
    label: str
    errors: list[str] = field(default_factory=list)


def worktree_is_done(worktree: Worktree) -> DoneSignal:
    """Whether ``worktree`` is teardown-eligible — necessary, but not sufficient.

    Reads the FSM state FIRST (no git), so a terminal ticket is done even when
    the local branch ref was deleted post-merge (the rc=128 case). Falls back to
    the forge squash-merge signal for a still-non-terminal ticket whose branch
    nonetheless shipped. Fail-safe to NOT done: a missing forge CLI or an
    inconclusive probe reads as not-done, so an uncertain worktree is kept.
    """
    ticket = worktree.ticket
    state = str(ticket.state) if ticket is not None else ""
    if state in _DONE_TICKET_STATES:
        return DoneSignal(done=True, source=f"ticket-state:{state}")
    if _branch_squash_merged(worktree):
        return DoneSignal(done=True, source="squash-merged")
    return DoneSignal(done=False, source=f"not-done:{state or 'no-ticket'}")


def _branch_squash_merged(worktree: Worktree) -> bool:
    """Whether the forge reports ``worktree``'s branch squash-merged. Fail-safe to False."""
    workspace = load_config().user.workspace_dir
    repo = resolve_clone_path(workspace, worktree)
    if repo is None or not repo.is_dir():
        return False
    try:
        default = git.default_branch(str(repo))
    except (RuntimeError, CommandFailedError):
        return False
    return is_squash_merged(str(repo), worktree.branch, default)


def analyze_worktree_changes(worktree: Worktree, *, workspace: Path) -> ChangeAnalysis:
    """Prove every uncommitted change and unpushed commit redundant, or keep the worktree.

    The PRIMARY safety step (CORRECTION 1 / the #706 data-loss guard hoisted): a
    done ticket is necessary but NOT sufficient to wipe. Two kinds of change are
    analysed against ``worktree``'s EFFECTIVE git target (resolved from git, not
    the possibly-drifted DB slug):

    - **Uncommitted changes** (ignoring the regenerable env cache) are never on
    any remote, so any real dirt marks the worktree potentially-needed.
    - **Unpushed commits** are proven redundant only by CURRENT-tip content:
    patch-id content-equivalence with ``origin/main`` (``git cherry``) or a
    superseding squash tree — never a merged-PR signal alone, which would destroy
    post-merge work. A branch-ref-gone (rc=128) worktree is decided from its
    recovered HEAD SHA — contained in a remote, or patch-id-equivalent to ``origin/main``.

    Fails CLOSED: every inconclusive probe contributes a kept-reason, so the
    worktree is kept rather than wiped on uncertainty.
    """
    wt_path = _resolve_worktree_path(workspace, worktree)
    repo_main = resolve_clone_path(workspace, worktree) or workspace / worktree.repo_path
    target = _effective_target(str(repo_main), wt_path, worktree)

    reasons: list[str] = []
    reasons.extend(_uncommitted_reasons(wt_path))
    reasons.extend(_unpushed_commit_reasons(Path(repo_main), target))
    return ChangeAnalysis(proven_redundant=not reasons, kept_reasons=reasons)


def _uncommitted_reasons(wt_path: str) -> list[str]:
    """Kept-reasons for real (non-regenerable) uncommitted changes; empty when clean.

    Fails CLOSED: an inconclusive ``git status`` (corrupt index, lock contention)
    is treated as dirty so the worktree is kept. A dangling-HEAD worktree (its
    branch ref deleted post-merge) is the exception: with no resolvable HEAD to
    diff against, ``git status`` reports EVERY tracked file as a staged addition —
    not real uncommitted work — so the dirty check is skipped and the recovered-HEAD
    commit analysis (:func:`_unpushed_commit_reasons`) decides that worktree instead.
    """
    if not Path(wt_path).is_dir():
        return []
    if not git.check(repo=wt_path, args=["rev-parse", "--verify", "--quiet", "HEAD"]):
        return []
    try:
        porcelain = git.status_porcelain(wt_path)
    except CommandFailedError as exc:
        return [f"could not read working-tree status ({exc}) — keeping"]
    dirty = [
        entry
        for line in porcelain.splitlines()
        if (entry := line[_PORCELAIN_STATUS_PREFIX_WIDTH:].strip())
        and not entry.startswith(_REGENERABLE_WORKTREE_PATHS)
    ]
    if not dirty:
        return []
    preview = ", ".join(dirty[:_PREVIEW_LIMIT]) + (", …" if len(dirty) > _PREVIEW_LIMIT else "")
    return [f"{len(dirty)} uncommitted change(s) not on any remote: {preview}"]


def _unpushed_commit_reasons(repo_main: Path, target: _EffectiveTarget) -> list[str]:
    """Kept-reasons for unpushed commits not proven redundant; empty when all redundant.

    Redundancy is decided by the CONTENT of the CURRENT tip, never by a "the branch
    once merged a PR" signal: people keep committing on a branch AFTER its PR merged,
    and those post-merge commits are NEW work bound for a fresh PR. So only two
    content-on-current-tip proofs authorise a wipe — every unique commit is patch-id
    present on ``origin/main`` (``git cherry``), or the tip's whole tree equals the
    squash/merge commit's tree. A merged PR whose source branch has since grown
    unique content is NOT sufficient (it would destroy the post-merge delta), so it
    is no longer consulted here — the worktree is kept and reported for salvage.
    """
    try:
        unpushed = git.commits_absent_from_all_remotes(target.probe_repo, target.ref)
    except CommandFailedError as exc:
        return _branch_ref_gone_reasons(target, exc)
    if not unpushed:
        return []
    branch = target.branch_to_delete
    content_ref = branch if branch is not None else target.ref
    content_repo = str(repo_main) if branch is not None else target.probe_repo
    if not content_equivalence_blockers(content_repo, content_ref):
        return []
    if branch is not None and _branch_tree_matches_squash(str(repo_main), branch):
        return []
    preview = ", ".join(unpushed[:_PREVIEW_LIMIT]) + (", …" if len(unpushed) > _PREVIEW_LIMIT else "")
    return [f"{len(unpushed)} commit(s) not provably on origin/main (content not upstream): {preview}"]


def _branch_ref_gone_reasons(target: _EffectiveTarget, exc: CommandFailedError) -> list[str]:
    """Decide the rc=128 (branch-ref-gone) case from the recovered HEAD — fail closed.

    A forge post-merge branch deletion leaves the worktree HEAD a dangling symref,
    so ``git log HEAD --not --remotes`` exits 128. The recovered HEAD SHA decides:
    contained in a remote (positive proof the work shipped) or patch-id-equivalent
    to ``origin/main`` (a squash captured it) is redundant; a recovered SHA on no
    remote with content NOT upstream is genuinely-ahead work (keep); an
    unrecoverable HEAD keeps the conservative "could not verify" refusal.
    """
    decision = classify_orphan_ref(target)
    if decision.in_remote:
        return []
    if decision.recovered_sha is None:
        return [f"could not verify the branch is pushed (git probe failed: {exc}) — keeping"]
    if not content_equivalence_blockers(target.probe_repo, decision.recovered_sha):
        return []
    count = len(decision.unsynced) or 1
    preview = ", ".join(decision.unsynced[:_PREVIEW_LIMIT]) or decision.recovered_sha[:7]
    return [f"{count} commit(s) on NO remote (content not upstream): {preview}"]


def reap_done_worktree(
    worktree: Worktree,
    *,
    workspace: Path,
    dry_run: bool,
) -> ReapOutcome:
    """Wipe one worktree only when done AND every change is proven redundant.

    The single per-worktree seam both ``clean-all`` and the FSM-automatic
    teardown funnel through. Order is load-bearing: ``clean_ignore`` skip →
    :func:`worktree_is_done` (necessary) → :func:`analyze_worktree_changes`
    (sufficient, primary safety) → wipe. A not-done or potentially-needed worktree
    is KEPT and reported, never snapshot. The wipe goes through
    :func:`cleanup_worktree` (git worktree + branch, the per-worktree DB, docker
    containers/images/volumes, overlay cleanup); the analysis already proved
    redundancy, so it runs with ``force=True`` (the analysis IS the data-loss
    gate) and ``strict_hygiene=False``.
    """
    if is_clean_ignored(worktree.branch, overlay=worktree.overlay):
        return ReapOutcome("skipped", f"SKIPPED '{worktree.branch}': matches clean_ignore — keeping")

    signal = worktree_is_done(worktree)
    if not signal.done:
        return ReapOutcome(
            "kept",
            f"KEPT '{worktree.branch}': not done ({signal.source}) — keeping the worktree",
        )

    analysis = analyze_worktree_changes(worktree, workspace=workspace)
    if not analysis.proven_redundant:
        return ReapOutcome(
            "kept",
            f"KEPT '{worktree.branch}': done ({signal.source}) but {'; '.join(analysis.kept_reasons)} "
            f"— salvage with `t3 <overlay> pr create`, do not wipe",
        )

    if dry_run:
        return ReapOutcome(
            "would-wipe",
            f"WOULD WIPE '{worktree.branch}': done ({signal.source}), all changes proven redundant",
        )

    result = cleanup_worktree(worktree, force=True, strict_hygiene=False)
    return ReapOutcome("wiped", f"Wiped '{worktree.branch}' ({signal.source}): {result.label}", errors=result.errors)


def reap_done_worktrees(workspace: Path, *, dry_run: bool) -> list[str]:
    """The one consolidated reaping pass — wipe every done+redundant worktree.

    Replaces the three former ``clean-all`` passes. Iterates every ``Worktree``
    row, keeps the not-done and potentially-needed ones with a reported reason, and
    wipes (or, under ``dry_run``, lists) the rest. The reaper is fully unattended
    by design (CORRECTION 3): an uncertain worktree is kept with a warning, never
    prompted — the old interactive push/abandon resolution is gone, salvage is a
    separate explicit action (``t3 <overlay> pr create``).

    DSLR snapshots are deliberately untouched: they are tenant baselines the
    snapshot worktrees restore FROM, not ticket-specific artifacts (CORRECTION 2),
    and are pruned by their own keep-latest-per-tenant pass.
    """
    return [
        reap_done_worktree(worktree, workspace=workspace, dry_run=dry_run).label
        for worktree in Worktree.objects.select_related("ticket")
    ]
