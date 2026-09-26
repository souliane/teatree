"""Done-detection + analyze-before-wipe, the one consolidated worktree reaping pass.

The redesign's core. Tearing a worktree down is destructive (git worktree +
branch removal, the per-worktree Postgres DB, docker containers/images AND
volumes), so the bar is two independent gates, both of which must pass:

1. ``worktree_is_done`` — the NECESSARY gate. A worktree is done only when its
ticket reached a genuinely-terminal state (``MERGED`` / ``DELIVERED`` /
``IGNORED`` — ``PR_OPENED`` is excluded: a PR is still open, the work is
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

from teatree.config import clone_root
from teatree.core.cleanup.cleanup import _effective_target, _EffectiveTarget, _resolve_worktree_path, cleanup_worktree
from teatree.core.cleanup.cleanup_emit import CleanupEmitRecord
from teatree.core.cleanup.cleanup_orphan_ref import classify_orphan_ref
from teatree.core.cleanup.reap_pre_gates import ReapPreGate, ReapPreGateVerdict, reap_pre_gate
from teatree.core.cleanup.unshipped_work import capture_unshipped_work
from teatree.core.cleanup.working_tree_dirt import real_uncommitted_reasons
from teatree.core.models import Ticket, Worktree
from teatree.core.worktree.branch_classification import (
    RedundancyVerdict,
    _branch_tree_matches_squash,
    branch_redundancy,
    content_equivalence_blockers,
    is_squash_merged,
    reset_forge_probe_cache,
)
from teatree.core.worktree.branch_verdict import branch_landed_for_teardown
from teatree.core.worktree.broken_checkout import BrokenCheckout, BrokenCheckoutVerdict, classify_broken_checkout
from teatree.core.worktree.clone_paths import resolve_clone_path
from teatree.core.worktree.worktree_emit import (
    _build_emit_record,
    _effective_default_target,
    _resolve_row_probes,
    _RowProbes,
)
from teatree.utils import git
from teatree.utils.run import CommandFailedError

logger = logging.getLogger(__name__)

# Terminal ticket states that authorise teardown. PR_OPENED is excluded on purpose
# — a shipped ticket still has an OPEN PR, so the work is not finished.
# REVIEW_DELIVERED (reviewer terminal) is included so a reviewer worktree is reaped.
# The canonical set lives on the model so the teardown signal (``core.signals``)
# and this reaper can never diverge on which states are terminal.
_DONE_TICKET_STATES = Ticket.marker_release_states()

_PREVIEW_LIMIT = 3
_FALLBACK_DEFAULT_TARGET = "origin/main"
_CLONE_UNRESOLVABLE_SOURCE = "clone-unresolvable"


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


def worktree_is_done(
    worktree: Worktree, *, branch: str | None = None, verdict: RedundancyVerdict | None = None
) -> DoneSignal:
    """Whether ``worktree`` is teardown-eligible — necessary, but not sufficient.

    Reads the FSM state FIRST (no git), so a terminal ticket is done even when
    the local branch ref was deleted post-merge (the rc=128 case). Falls back to
    the forge squash-merge signal for a still-non-terminal ticket whose branch
    nonetheless shipped. Fail-safe to NOT done: a missing forge CLI or an
    inconclusive probe reads as not-done, so an uncertain worktree is kept.

    ``branch`` is the branch the CHECKOUT actually holds, which the DB slug can
    drift from. Judging the slug asks about a branch nobody is working on: it
    reported ``squash-merged`` for a checkout holding unlanded commits on a
    different branch, which is a done signal a sweep acts on. Defaults to the slug
    for the callers that have no resolved target.
    """
    ticket = worktree.ticket
    state = str(ticket.state) if ticket is not None else ""
    if state in _DONE_TICKET_STATES:
        return DoneSignal(done=True, source=f"ticket-state:{state}")
    if _branch_squash_merged(worktree, branch or worktree.branch, verdict=verdict):
        return DoneSignal(done=True, source="squash-merged")
    return DoneSignal(done=False, source=f"not-done:{state or 'no-ticket'}")


def _branch_squash_merged(worktree: Worktree, branch: str, *, verdict: RedundancyVerdict | None = None) -> bool:
    """Whether ``branch`` is provably squash-merged AND has no open PR. Fail-safe to False.

    The content heuristic (:func:`is_squash_merged`) matches any branch whose tip is
    patch-id-equivalent to ``origin/<default>`` — including a still-OPEN PR that merely
    resembles the default branch. An open PR is the forge's positive proof the work is
    unfinished, so it vetoes the squash-merged done signal (#3093): a worktree backing an
    open PR is never reported done, so a sweep can never wipe its live work. The veto
    lives inside :func:`is_squash_merged` (the shared destructive chokepoint), so this
    path and the branch-prune pass inherit it identically. The FSM terminal-state path
    in :func:`worktree_is_done` is unaffected — only this content heuristic is gated.

    A ``verdict`` the caller already ran is the SAME ladder over the same branch, so
    reusing it changes no answer and drops one forge round-trip per row. It is read
    only once the clone resolves: a verdict computed against a fallback path that holds
    no clone speaks for nothing, and a not-done ticket is never done on that guess.
    """
    workspace = clone_root()
    repo = resolve_clone_path(workspace, worktree)
    if repo is None or not repo.is_dir():
        return False
    if verdict is not None:
        return verdict.redundant
    try:
        default = git.default_branch(str(repo))
    except (RuntimeError, CommandFailedError):
        return False
    return is_squash_merged(str(repo), branch, default)


def analyze_worktree_changes(
    worktree: Worktree, *, workspace: Path, probes: _RowProbes | None = None
) -> ChangeAnalysis:
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

    ``probes`` are the row-level resolutions the reaper already made; they are
    recomputed here only for a caller that has none.
    """
    wt_path = _resolve_worktree_path(workspace, worktree)
    repo_main = resolve_clone_path(workspace, worktree) or workspace / worktree.repo_path
    target = probes.target if probes else _effective_target(str(repo_main), wt_path, worktree)
    default_target = _effective_default_target(Path(repo_main))

    reasons: list[str] = []
    reasons.extend(real_uncommitted_reasons(wt_path, target))
    reasons.extend(
        _unpushed_commit_reasons(
            Path(repo_main), target, default_target=default_target, verdict=probes.verdict if probes else None
        )
    )
    return ChangeAnalysis(proven_redundant=not reasons, kept_reasons=reasons)


def _wipe_fingerprint(
    worktree: Worktree, *, workspace: Path, probes: _RowProbes | None = None
) -> tuple[str | None, tuple[str, ...]]:
    """The tip SHA plus the working tree's dirt — the state the analysis was made against.

    The TOCTOU bracket for :func:`reap_done_worktree`: sampled before the
    redundancy analysis and again just before the force-wipe, so anything landing
    in the window changes the value and the wipe is refused. BOTH halves are
    carried because :func:`analyze_worktree_changes` proves both redundant — a
    tip-only bracket left an uncommitted edit written mid-sweep to be force-wiped
    unexamined. A present ref resolves via ``rev-parse``; a dangling HEAD
    (post-merge ref deletion) falls back to the reflog-recovered SHA so a moving
    dangling ref is still detected.
    """
    wt_path = _resolve_worktree_path(workspace, worktree)
    repo_main = resolve_clone_path(workspace, worktree) or workspace / worktree.repo_path
    target = probes.target if probes else _effective_target(str(repo_main), wt_path, worktree)
    resolved = git.run(repo=target.probe_repo, args=["rev-parse", "--verify", "--quiet", target.ref])
    head = resolved or classify_orphan_ref(target).recovered_sha
    return head, tuple(real_uncommitted_reasons(wt_path, target))


def _unpushed_commit_reasons(
    repo_main: Path,
    target: _EffectiveTarget,
    *,
    default_target: str = _FALLBACK_DEFAULT_TARGET,
    verdict: RedundancyVerdict | None = None,
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
    reported for salvage. The layered ladder is the third proof, and only ANDed with
    present-tense presence — see the comment at its rung.
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
    # Neither rung below carries the #4719 presence conjunct, deliberately: each proves the
    # branch's OWN commits are on the target's PUSHED history — every one patch-id-matched, or
    # the tip tree identical to the forge's merge commit — so a copy outlives the teardown.
    if not content_equivalence_blockers(content_repo, content_ref, default_target):
        return []
    if branch is not None and _branch_tree_matches_squash(str(repo_main), branch):
        return []
    # The full landed ladder, for the branch the two probes above cannot clear:
    # a squash whose patch was resolved at merge and whose file the base then
    # edited again defeats every patch-id/tree instrument, while the forge's
    # merge record at the exact tip still proves it landed. Fails CLOSED.
    #
    # Its git-local rungs are ANDed with present-tense presence (#4719): this
    # analysis IS the data-loss gate — its caller force-wipes past every guard in
    # ``cleanup_worktree`` — and those rungs read a patch's PRIOR appearance, which
    # a later commit over the same region does not erase.
    if branch is not None:
        landed = verdict if verdict is not None else branch_redundancy(content_repo, branch, default_target)
        if landed.redundant and branch_landed_for_teardown(content_repo, branch, default_target):
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


def _pre_gate_outcome(worktree: Worktree, *, workspace: Path, verdict: ReapPreGateVerdict) -> ReapOutcome:
    """Render a shared :func:`reap_pre_gate` verdict in this pass's own vocabulary.

    A ``clean_ignore`` skip carries no emit record: the operator has already ruled
    the branch never-reap, so there is nothing for the judgment skill to route. The
    other two do, so an EXCLUDED or ACTIVE item still reaches the skill.
    """
    if verdict.gate is ReapPreGate.CLEAN_IGNORE:
        return ReapOutcome("skipped", f"SKIPPED '{worktree.branch}': {verdict.reason}")
    if verdict.gate is ReapPreGate.OWNERSHIP:
        return ReapOutcome(
            "excluded",
            f"EXCLUDED '{worktree.branch}': {verdict.reason}",
            emit=_build_emit_record(worktree, workspace=workspace, liveness=""),
        )
    return ReapOutcome(
        "active",
        f"ACTIVE '{worktree.branch}': {verdict.reason} — skipping (do not wipe a live item)",
        emit=_build_emit_record(worktree, workspace=workspace, liveness=verdict.reason),
    )


def reap_done_worktree(
    worktree: Worktree,
    *,
    workspace: Path,
    dry_run: bool,
    fsm_terminal: bool = False,
) -> ReapOutcome:
    """Wipe one worktree only when owned, not live, done AND every change proven redundant.

    The single per-worktree seam both ``clean-all`` and the FSM-automatic
    teardown funnel through. Order is load-bearing: the shared
    :func:`~teatree.core.cleanup.reap_pre_gates.reap_pre_gate` protection gates
    (``clean_ignore`` skip → OWNERSHIP → LIVENESS, the same predicate the narrow
    ``workspace release-dead-rows`` pass consults) → :func:`worktree_is_done`
    (necessary) → :func:`analyze_worktree_changes` (sufficient, primary safety) →
    wipe. Every item NOT auto-deleted carries a structured ``emit`` record for the
    judgment skill; only a provably-redundant item is wiped (``force=True`` — the
    analysis IS the data-loss gate — ``strict_hygiene=False``).

    ``fsm_terminal`` marks the post-merge FSM-immediate teardown (``WorktreeTeardown``
    on the merge transition): the LIVENESS guard then bypasses the two signals the
    merge ceremony itself trips (busy-ticket from the new phase session, recent-commit
    from the merge commit) so a just-merged worktree is actually reaped. The
    data-loss gate (:func:`analyze_worktree_changes`) is unchanged — a dirty or
    genuinely-ahead worktree is still KEPT on the FSM path. The ad-hoc ``clean-all``
    sweep leaves ``fsm_terminal`` off, preserving the full live-work protection.

    The capture runs before the FIRST return, so it covers every disposition —
    including the KEPT ones, which is where work accumulated unobserved (#4272).
    Capturing only inside teardown meant the one disposition that tears nothing
    down, a row whose ticket is still open, wrote no record at all: 75 of 77
    registered rows on the reporting host, the worst holding 25 modified files
    with no commit and no remote branch. It reads the checkout and writes
    elsewhere and never raises, so no verdict below depends on it; it is skipped
    under ``dry_run`` to keep a preview free of side effects.
    """
    if not dry_run:
        capture_unshipped_work(
            Path(_resolve_worktree_path(workspace, worktree)), branch=worktree.branch, overlay=worktree.overlay
        )
    pre_gate = reap_pre_gate(worktree, workspace=workspace, fsm_terminal=fsm_terminal)
    if pre_gate is not None:
        return _pre_gate_outcome(worktree, workspace=workspace, verdict=pre_gate)

    broken = classify_broken_checkout(worktree, workspace=workspace)
    if broken.state is not BrokenCheckout.LIVE_CHECKOUT:
        return _dead_checkout_outcome(worktree, workspace=workspace, verdict=broken, dry_run=dry_run)

    wt_path = _resolve_worktree_path(workspace, worktree)
    repo_main = resolve_clone_path(workspace, worktree) or workspace / worktree.repo_path
    probes = _resolve_row_probes(workspace, Path(repo_main), wt_path, worktree)

    branch = probes.target.branch_to_delete or worktree.branch
    signal = worktree_is_done(worktree, branch=branch, verdict=probes.verdict)
    if not signal.done:
        return ReapOutcome(
            "kept",
            f"KEPT '{worktree.branch}': not done ({signal.source}) — keeping the worktree",
            emit=_build_emit_record(worktree, workspace=workspace, liveness="", probes=probes),
        )

    fingerprint_at_analysis = _wipe_fingerprint(worktree, workspace=workspace, probes=probes)
    analysis = analyze_worktree_changes(worktree, workspace=workspace, probes=probes)
    if not analysis.proven_redundant:
        return ReapOutcome(
            "kept",
            f"KEPT '{worktree.branch}': done ({signal.source}) but {'; '.join(analysis.kept_reasons)} "
            f"— salvage with `t3 <overlay> workspace salvage`, do not wipe",
            emit=_build_emit_record(worktree, workspace=workspace, liveness="", probes=probes),
        )

    return _wipe_proven_redundant(
        worktree, signal=signal, fingerprint_at_analysis=fingerprint_at_analysis, dry_run=dry_run, probes=probes
    )


def _dead_checkout_outcome(
    worktree: Worktree,
    *,
    workspace: Path,
    verdict: BrokenCheckoutVerdict,
    dry_run: bool,
) -> ReapOutcome:
    """Release (or keep) a row whose checkout is dead — the ONE owner of that state.

    The done/redundancy gates below cannot reach a dir git will not open, so this
    branch decides instead, on the proof :func:`classify_broken_checkout` gathered
    from the source clone. ``force=True`` is warranted for the same reason it is in
    :func:`_wipe_proven_redundant`: that proof IS the data-loss gate, and the guards
    it bypasses are exactly the ones that can only fail closed here. The dir itself
    SURVIVES: nothing in ``clean-all`` removes a checkout directory under a worktree
    root any more, because no evidence one execution context can gather proves a
    checkout dead everywhere (#3912). Disposing of its files is an explicit operator
    decision, reached through ``workspace salvage``.
    """
    if verdict.state is not BrokenCheckout.RELEASABLE:
        return ReapOutcome(
            "kept",
            f"KEPT '{worktree.branch}': {verdict.reason}",
            emit=_build_emit_record(worktree, workspace=workspace, liveness=""),
        )
    if dry_run:
        return ReapOutcome("would-wipe", f"WOULD RELEASE '{worktree.branch}': {verdict.reason}")
    result = cleanup_worktree(worktree, force=True, strict_hygiene=False)
    return ReapOutcome("wiped", f"Released dead checkout '{worktree.branch}': {result.label}", errors=result.errors)


def _fingerprint_label(fingerprint: tuple[str | None, tuple[str, ...]]) -> str:
    head, dirt = fingerprint
    return f"{head} + {len(dirt)} dirt reason(s)"


def _wipe_proven_redundant(
    worktree: Worktree,
    *,
    signal: DoneSignal,
    fingerprint_at_analysis: tuple[str | None, tuple[str, ...]],
    dry_run: bool,
    probes: _RowProbes,
) -> ReapOutcome:
    """Wipe a proven-redundant worktree, re-checking the TOCTOU bracket before the force-wipe.

    The redundancy analysis proved the state captured at ``fingerprint_at_analysis``
    is fully upstream, but ``cleanup_worktree(force=True)`` bypasses every data-loss
    guard. A commit OR an uncommitted edit landing between the analysis and the wipe
    would be destroyed unexamined — so the fingerprint is re-read here and the wipe
    refused (KEEP) if it moved, leaving the worktree for the next sweep to re-analyse.
    """
    if dry_run:
        return ReapOutcome(
            "would-wipe", f"WOULD WIPE '{worktree.branch}': done ({signal.source}), all changes proven redundant"
        )
    fingerprint_before_wipe = _wipe_fingerprint(worktree, workspace=probes.workspace, probes=probes)
    if fingerprint_before_wipe != fingerprint_at_analysis:
        return ReapOutcome(
            "kept",
            f"KEPT '{worktree.branch}': the worktree changed during analysis "
            f"({_fingerprint_label(fingerprint_at_analysis)} → {_fingerprint_label(fingerprint_before_wipe)}) "
            "— re-run cleanup to re-analyse",
            emit=_build_emit_record(worktree, workspace=probes.workspace, liveness="", probes=probes),
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
    reset_forge_probe_cache()
    return [
        _reap_or_report(worktree, workspace=workspace, dry_run=dry_run)
        for worktree in Worktree.objects.select_related("ticket")
    ]


def _reap_or_report(worktree: Worktree, *, workspace: Path, dry_run: bool) -> ReapOutcome:
    """One row's disposition, or an ``error`` outcome — a raising row never aborts the sweep.

    A single unreadable row used to abort the whole first pass, so every row after
    it went unexamined and the backlog could only ever grow.
    """
    try:
        return reap_done_worktree(worktree, workspace=workspace, dry_run=dry_run)
    except Exception as exc:  # one bad row must not cost the sweep every row after it
        logger.exception("reaping wt#%s failed", worktree.pk)
        return ReapOutcome("error", f"ERROR wt#{worktree.pk} '{worktree.branch}': {exc!r} — row skipped, nothing wiped")


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
