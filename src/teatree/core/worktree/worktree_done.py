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
is KEPT and reported, never wiped (salvage — push-to-PR via ``t3 <overlay> pr
create`` — is a separate action). The analysis fails CLOSED: an inconclusive git
probe keeps it.

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

from teatree.config import clone_root, get_effective_settings
from teatree.core.cleanup.clean_ignore import is_clean_ignored
from teatree.core.cleanup.cleanup import _effective_target, _EffectiveTarget, _resolve_worktree_path, cleanup_worktree
from teatree.core.cleanup.cleanup_emit import CleanupEmitRecord, banned_terms_status
from teatree.core.cleanup.cleanup_liveness import worktree_liveness
from teatree.core.cleanup.cleanup_orphan_ref import classify_orphan_ref
from teatree.core.cleanup.cleanup_ownership import is_excluded_by_ownership
from teatree.core.cleanup.working_tree_dirt import real_uncommitted_reasons
from teatree.core.models import Ticket, Worktree
from teatree.core.worktree.branch_classification import (
    _branch_has_open_pr,
    _branch_tree_matches_squash,
    branch_redundancy,
    content_equivalence_blockers,
    effective_default_target,
    is_squash_merged,
)
from teatree.core.worktree.clone_paths import resolve_clone_path
from teatree.utils import git
from teatree.utils.run import CommandFailedError

logger = logging.getLogger(__name__)

# Terminal ticket states that authorise teardown. SHIPPED is excluded on purpose
# — a shipped ticket still has an OPEN PR, so the work is not finished.
# REVIEW_POSTED (reviewer terminal) is included so a reviewer worktree is reaped.
_DONE_TICKET_STATES = frozenset(
    {Ticket.State.MERGED, Ticket.State.DELIVERED, Ticket.State.REVIEW_POSTED, Ticket.State.IGNORED},
)

_PREVIEW_LIMIT = 3
_FALLBACK_DEFAULT_TARGET = "origin/main"


def _effective_default_target(repo: Path) -> str:
    """Resolve ``repo``'s REAL default branch as an ``origin/<default>`` ref.

    Thin ``Path``-taking adapter over the shared
    :func:`branch_classification.effective_default_target` so done-detection, the
    redundancy/emit probes, and :func:`cleanup._raise_if_genuinely_ahead` all
    resolve the base the SAME way (a ``master``/``develop``-default repo is never
    measured against a base it does not have). Fail-safe to ``origin/main`` on an
    unresolvable default — the downstream content gate fails CLOSED there.
    """
    return effective_default_target(str(repo))


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
    (clean_ignore) / ``excluded`` (colleague-owned) / ``active`` (live). ``label``
    is the human-readable result line. ``errors`` carries any non-fatal
    teardown-step failure surfaced by :func:`cleanup_worktree`. ``emit`` is the
    structured handoff record for the judgment skill, set on every item the CLI
    did NOT auto-delete (``None`` for wiped / would-wipe / clean_ignore-skipped).
    """

    action: str
    label: str
    errors: list[str] = field(default_factory=list)
    emit: CleanupEmitRecord | None = None


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
    """Whether ``worktree``'s branch is provably squash-merged AND has no open PR. Fail-safe to False.

    The content heuristic (:func:`is_squash_merged`) matches any branch whose tip is
    patch-id-equivalent to ``origin/<default>`` — including a still-OPEN PR that merely
    resembles the default branch. An open PR is the forge's positive proof the work is
    unfinished, so it vetoes the squash-merged done signal (#3093): a worktree backing an
    open PR is never reported done, so a sweep can never wipe its live work. The FSM
    terminal-state path in :func:`worktree_is_done` is unaffected — only this content
    heuristic is gated.
    """
    workspace = clone_root()
    repo = resolve_clone_path(workspace, worktree)
    if repo is None or not repo.is_dir():
        return False
    try:
        default = git.default_branch(str(repo))
    except (RuntimeError, CommandFailedError):
        return False
    if _branch_has_open_pr(str(repo), worktree.branch):
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
    default_target = _effective_default_target(Path(repo_main))

    reasons: list[str] = []
    reasons.extend(real_uncommitted_reasons(wt_path, target))
    reasons.extend(_unpushed_commit_reasons(Path(repo_main), target, default_target=default_target))
    return ChangeAnalysis(proven_redundant=not reasons, kept_reasons=reasons)


def _current_head_sha(worktree: Worktree, *, workspace: Path) -> str | None:
    """The worktree's current tip SHA, or the recovered last-HEAD SHA when the ref is gone.

    The TOCTOU bracket for :func:`reap_done_worktree`: sampled before the
    redundancy analysis and again just before the force-wipe, so a commit that
    lands in the window changes the value and the wipe is refused. A present ref
    resolves via ``rev-parse``; a dangling HEAD (post-merge ref deletion) falls
    back to the reflog-recovered SHA so a moving dangling ref is still detected.
    """
    wt_path = _resolve_worktree_path(workspace, worktree)
    repo_main = resolve_clone_path(workspace, worktree) or workspace / worktree.repo_path
    target = _effective_target(str(repo_main), wt_path, worktree)
    resolved = git.run(repo=target.probe_repo, args=["rev-parse", "--verify", "--quiet", target.ref])
    if resolved:
        return resolved
    return classify_orphan_ref(target).recovered_sha


def _unpushed_commit_reasons(
    repo_main: Path, target: _EffectiveTarget, *, default_target: str = _FALLBACK_DEFAULT_TARGET
) -> list[str]:
    """Kept-reasons for unpushed commits not proven redundant; empty when all redundant.

    Redundancy is decided by the CONTENT of the CURRENT tip, never by a "the branch
    once merged a PR" signal: people keep committing on a branch AFTER its PR merged,
    and those post-merge commits are NEW work bound for a fresh PR. So only two
    content-on-current-tip proofs authorise a wipe — every unique commit is patch-id
    present on ``default_target`` (the repo's REAL default, ``git cherry``), or the
    tip's whole tree equals the squash/merge commit's tree. A merged PR whose source
    branch has since grown unique content is NOT sufficient (it would destroy the
    post-merge delta), so it is no longer consulted here — the worktree is kept and
    reported for salvage.
    """
    try:
        unpushed = git.commits_absent_from_all_remotes(target.probe_repo, target.ref)
    except CommandFailedError as exc:
        return _branch_ref_gone_reasons(target, exc, default_target=default_target)
    if not unpushed:
        return []
    branch = target.branch_to_delete
    content_ref = branch if branch is not None else target.ref
    content_repo = str(repo_main) if branch is not None else target.probe_repo
    if not content_equivalence_blockers(content_repo, content_ref, default_target):
        return []
    if branch is not None and _branch_tree_matches_squash(str(repo_main), branch):
        return []
    preview = ", ".join(unpushed[:_PREVIEW_LIMIT]) + (", …" if len(unpushed) > _PREVIEW_LIMIT else "")
    return [f"{len(unpushed)} commit(s) not provably on {default_target} (content not upstream): {preview}"]


def _branch_ref_gone_reasons(
    target: _EffectiveTarget, exc: CommandFailedError, *, default_target: str = _FALLBACK_DEFAULT_TARGET
) -> list[str]:
    """Decide the rc=128 (branch-ref-gone) case from the recovered HEAD — fail closed.

    A forge post-merge branch deletion leaves the worktree HEAD a dangling symref,
    so ``git log HEAD --not --remotes`` exits 128. The recovered HEAD SHA decides:
    contained in a remote (positive proof the work shipped) or patch-id-equivalent
    to ``default_target`` (a squash captured it) is redundant; a recovered SHA on no
    remote with content NOT upstream is genuinely-ahead work (keep); an
    unrecoverable HEAD keeps the conservative "could not verify" refusal.
    """
    decision = classify_orphan_ref(target)
    if decision.in_remote:
        return []
    if decision.recovered_sha is None:
        return [f"could not verify the branch is pushed (git probe failed: {exc}) — keeping"]
    if not content_equivalence_blockers(target.probe_repo, decision.recovered_sha, default_target):
        return []
    count = len(decision.unsynced) or 1
    preview = ", ".join(decision.unsynced[:_PREVIEW_LIMIT]) or decision.recovered_sha[:7]
    return [f"{count} commit(s) on NO remote (content not upstream): {preview}"]


def _build_emit_record(worktree: Worktree, *, workspace: Path, liveness: str) -> CleanupEmitRecord:
    """Assemble the structured handoff record for a NOT-auto-deleted worktree.

    Resolves the current-tip redundancy (for ``unique_commit_shas`` +
    ``merged_with_post_merge_work``), the banned-terms status of the unique
    content, the tip author/date, and the liveness reason — everything the
    judgment skill needs to route the item without re-probing git itself.
    """
    wt_path = _resolve_worktree_path(workspace, worktree)
    repo_main = resolve_clone_path(workspace, worktree) or workspace / worktree.repo_path
    target = _effective_target(str(repo_main), wt_path, worktree)
    ref = target.branch_to_delete or worktree.branch
    probe_repo = str(repo_main)
    default_target = _effective_default_target(Path(repo_main))
    verdict = branch_redundancy(probe_repo, ref, default_target)
    try:
        texts = [
            git.run_strict(repo=probe_repo, args=["log", f"{default_target}..{ref}", "--format=%B"]),
            git.run_strict(repo=probe_repo, args=["diff", f"{default_target}...{ref}"]),
        ]
    except CommandFailedError:
        # STRICT so the failure is real, not a lenient "" degrade. Unreadable
        # content emits banned_terms_status "unknown" — the judgment skill treats
        # an unknown-scan item conservatively (clean before salvage), never as
        # "scanned clean".
        texts = []
    status, found = banned_terms_status(texts)
    owner = git.run(repo=probe_repo, args=["log", "-1", "--format=%an", ref])
    last_date = git.run(repo=probe_repo, args=["log", "-1", "--format=%cI", ref])
    return CleanupEmitRecord(
        path=wt_path,
        branch=worktree.branch,
        kind="worktree",
        unique_commit_shas=verdict.unique_shas,
        merged_with_post_merge_work=verdict.merged_with_post_merge_work,
        banned_terms_status=status,
        banned_terms_found=found,
        liveness=liveness,
        last_commit_date=last_date,
        owner=owner,
    )


def _ownership_liveness_skip(worktree: Worktree, *, workspace: Path, fsm_terminal: bool = False) -> ReapOutcome | None:
    """The OWNERSHIP then LIVENESS pre-gate: a skip :class:`ReapOutcome`, or ``None`` to proceed.

    A colleague's work on a product repo is EXCLUDED up front; an actively-worked
    item is skipped-as-ACTIVE. Both carry a structured emit record so the skill
    sees them. ``None`` means neither gate fired and the reaper may continue to
    done-detection. ``fsm_terminal`` is threaded to :func:`worktree_liveness` so
    the post-merge teardown bypasses the FSM-ceremony false positives (the merge
    that just landed mints the phase session and the merge commit).
    """
    wt_path = _resolve_worktree_path(workspace, worktree)
    repo_main = resolve_clone_path(workspace, worktree) or workspace / worktree.repo_path
    settings = get_effective_settings()
    ownership = is_excluded_by_ownership(
        str(repo_main),
        worktree.branch,
        owner_aliases=settings.user_identity_aliases,
        colleague_pattern=settings.colleague_repo_url_pattern,
    )
    if ownership.excluded:
        return ReapOutcome(
            "excluded",
            f"EXCLUDED '{worktree.branch}': {ownership.reason}",
            emit=_build_emit_record(worktree, workspace=workspace, liveness=""),
        )
    liveness = worktree_liveness(worktree, wt_path=Path(wt_path), fsm_terminal=fsm_terminal)
    if liveness.active:
        return ReapOutcome(
            "active",
            f"ACTIVE '{worktree.branch}': {liveness.reason} — skipping (do not wipe a live item)",
            emit=_build_emit_record(worktree, workspace=workspace, liveness=liveness.reason),
        )
    return None


def reap_done_worktree(
    worktree: Worktree,
    *,
    workspace: Path,
    dry_run: bool,
    fsm_terminal: bool = False,
) -> ReapOutcome:
    """Wipe one worktree only when owned, not live, done AND every change proven redundant.

    The single per-worktree seam both ``clean-all`` and the FSM-automatic
    teardown funnel through. Order is load-bearing: ``clean_ignore`` skip →
    OWNERSHIP guard (exclude a colleague's work on a product repo) → LIVENESS guard
    (skip an actively-worked item) → :func:`worktree_is_done` (necessary) →
    :func:`analyze_worktree_changes` (sufficient, primary safety) → wipe. Every
    item NOT auto-deleted carries a structured ``emit`` record for the judgment
    skill; only a provably-redundant item is wiped (``force=True`` —  the analysis
    IS the data-loss gate — ``strict_hygiene=False``).

    ``fsm_terminal`` marks the post-merge FSM-immediate teardown (``WorktreeTeardown``
    on the merge transition): the LIVENESS guard then bypasses the two signals the
    merge ceremony itself trips (busy-ticket from the new phase session, recent-commit
    from the merge commit) so a just-merged worktree is actually reaped. The
    data-loss gate (:func:`analyze_worktree_changes`) is unchanged — a dirty or
    genuinely-ahead worktree is still KEPT on the FSM path. The ad-hoc ``clean-all``
    sweep leaves ``fsm_terminal`` off, preserving the full live-work protection.
    """
    if is_clean_ignored(worktree.branch, overlay=worktree.overlay):
        return ReapOutcome("skipped", f"SKIPPED '{worktree.branch}': matches clean_ignore — keeping")

    pre_gate = _ownership_liveness_skip(worktree, workspace=workspace, fsm_terminal=fsm_terminal)
    if pre_gate is not None:
        return pre_gate

    signal = worktree_is_done(worktree)
    if not signal.done:
        return ReapOutcome(
            "kept",
            f"KEPT '{worktree.branch}': not done ({signal.source}) — keeping the worktree",
            emit=_build_emit_record(worktree, workspace=workspace, liveness=""),
        )

    head_at_analysis = _current_head_sha(worktree, workspace=workspace)
    analysis = analyze_worktree_changes(worktree, workspace=workspace)
    if not analysis.proven_redundant:
        return ReapOutcome(
            "kept",
            f"KEPT '{worktree.branch}': done ({signal.source}) but {'; '.join(analysis.kept_reasons)} "
            f"— salvage with `t3 <overlay> workspace salvage`, do not wipe",
            emit=_build_emit_record(worktree, workspace=workspace, liveness=""),
        )

    return _wipe_proven_redundant(
        worktree, workspace=workspace, signal=signal, head_at_analysis=head_at_analysis, dry_run=dry_run
    )


def _wipe_proven_redundant(
    worktree: Worktree,
    *,
    workspace: Path,
    signal: DoneSignal,
    head_at_analysis: str | None,
    dry_run: bool,
) -> ReapOutcome:
    """Wipe a proven-redundant worktree, with a TOCTOU re-check of HEAD before the force-wipe.

    The redundancy analysis proved the tip captured at ``head_at_analysis`` is
    fully upstream, but ``cleanup_worktree(force=True)`` bypasses every data-loss
    guard. A commit landing between the analysis and the wipe would be destroyed
    unexamined — so HEAD is re-read here and the wipe refused (KEEP) if it moved,
    leaving the worktree for the next sweep to re-analyse.
    """
    if dry_run:
        return ReapOutcome(
            "would-wipe",
            f"WOULD WIPE '{worktree.branch}': done ({signal.source}), all changes proven redundant",
        )
    head_before_wipe = _current_head_sha(worktree, workspace=workspace)
    if head_before_wipe != head_at_analysis:
        return ReapOutcome(
            "kept",
            f"KEPT '{worktree.branch}': HEAD moved during analysis "
            f"({head_at_analysis} → {head_before_wipe}) — re-run cleanup to re-analyse",
            emit=_build_emit_record(worktree, workspace=workspace, liveness=""),
        )
    result = cleanup_worktree(worktree, force=True, strict_hygiene=False)
    return ReapOutcome("wiped", f"Wiped '{worktree.branch}' ({signal.source}): {result.label}", errors=result.errors)


def reap_done_worktrees_detailed(workspace: Path, *, dry_run: bool) -> list[ReapOutcome]:
    """The one consolidated reaping pass — full :class:`ReapOutcome` per Worktree row.

    Replaces the three former ``clean-all`` passes. Iterates every ``Worktree``
    row; wipes (or, under ``dry_run``, lists) only the owned, non-live,
    done+redundant ones and KEEPS/EXCLUDES/skips-as-ACTIVE the rest — each with a
    structured ``emit`` record for the judgment skill. Fully unattended (CORRECTION
    3): never prompts, salvage is the separate explicit ``t3 <overlay> workspace
    salvage``. DSLR snapshots are deliberately untouched (CORRECTION 2).
    """
    return [
        reap_done_worktree(worktree, workspace=workspace, dry_run=dry_run)
        for worktree in Worktree.objects.select_related("ticket")
    ]


def reap_done_worktrees(workspace: Path, *, dry_run: bool) -> list[str]:
    """The label-only view of :func:`reap_done_worktrees_detailed` (back-compat for the CLI)."""
    return [outcome.label for outcome in reap_done_worktrees_detailed(workspace, dry_run=dry_run)]


def collect_emit_records(workspace: Path) -> list[CleanupEmitRecord]:
    """The structured handoff for the judgment skill — one record per NOT-auto-deleted item.

    A read-only pass (``dry_run=True`` so nothing is wiped) that returns the
    machine-readable EMIT records the skill consumes (it serialises them to JSON).
    """
    return [
        outcome.emit for outcome in reap_done_worktrees_detailed(workspace, dry_run=True) if outcome.emit is not None
    ]
