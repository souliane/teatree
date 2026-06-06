"""Pre-ship gate helpers, extracted from ``pr.py`` by concern.

The ``pr create`` command runs a sequence of deterministic gates before it
advances the FSM to SHIPPED: branch-currency auto-merge (#940), the
phase/shipping gate (#694), the overlay-scoped close-keyword gates (#1012 /
#83), the visual-QA browser sanity gate, and the PR-metadata validator. Those
leaf gates plus their failure payloads live here so ``pr.py`` stays within the
module-health LOC bar; ``pr.py`` re-imports them and keeps the ``_run_ship_gates``
orchestrator (so the existing ``patch.object(pr, "_run_visual_qa_gate")`` test
seams keep resolving against ``pr``'s namespace).
"""

from typing import TypedDict, cast

from teatree import visual_qa
from teatree.core.branch_currency import require_current_branch
from teatree.core.gates.e2e_mandatory_gate import E2EMandatoryGateError, check_e2e_mandatory, resolve_gate_inputs
from teatree.core.management.commands._ship_exec import ShippingGateFailure
from teatree.core.management.commands._ship_fsm import reconcile_fsm_for_ship
from teatree.core.models import Session, Ticket, Worktree
from teatree.core.models.types import TicketExtra, VisualQASummary
from teatree.core.overlay_loader import get_overlay
from teatree.core.phases import normalize_phase
from teatree.core.runners.ship import resolve_ship_worktree
from teatree.utils import git
from teatree.utils.run import CommandFailedError


class VisualQAGateFailure(TypedDict):
    allowed: bool
    error: str
    visual_qa: VisualQASummary
    report_markdown: str
    hint: str


class BranchCurrencyFailure(TypedDict):
    """Pre-ship branch-currency gate refusal (#940).

    Returned when ``origin/<target>`` has advanced past the branch
    point and ``git merge`` produced conflicts. The branch must be
    manually merged + resolved before the cold reviewer attests the
    post-merge SHA — otherwise the release pipeline certifies a stale
    base.
    """

    allowed: bool
    error: str
    hint: str
    branch: str
    target: str


class NoCommitsAheadError(TypedDict):
    error: str
    branch: str
    base: str


def assert_commits_ahead_of_base(worktree: Worktree) -> NoCommitsAheadError | None:
    """Block a hollow ship: the branch must have ≥1 commit ahead of base (#788).

    The phase gate answers "is the work attested?"; this answers "is
    there actually anything to ship?". Without it, a
    reviewer-approved-but-uncommitted (or committed-elsewhere) branch
    produced a hollow ``state: shipped`` — the CLI reported success, the
    FSM advanced, then ``execute_ship`` later failed with "No commits
    between main and branch", deferring the real failure into the async
    worker where it is easy to miss.

    Blocks **only on a confirmed zero** (``rev_count(base..branch) ==
    0``), naming the branch and the base it was compared against. If git
    introspection cannot be performed (no real repo, base undetectable,
    git error) the state is *unverifiable* — distinct from the
    confirmed-zero bug — so the prior behaviour (proceed) is preserved
    rather than blocking on an unknown.
    """
    repo = (worktree.extra or {}).get("worktree_path", "") or worktree.repo_path
    branch = worktree.branch
    if not repo or not branch:
        return None
    try:
        base = f"origin/{git.default_branch(repo=repo)}"
        ahead = git.rev_count(repo=repo, range_spec=f"{base}..{branch}")
    except (CommandFailedError, RuntimeError, ValueError):
        return None  # unverifiable ≠ the confirmed-zero bug — do not block
    if ahead > 0:
        return None
    return NoCommitsAheadError(
        error=(
            f"Refusing to ship: branch {branch!r} has 0 commits ahead of {base} — "
            f"nothing to push (work uncommitted or committed elsewhere). "
            f"Commit the work on {branch!r}, then retry `pr create`."
        ),
        branch=branch,
        base=base,
    )


def resolve_target_branch(ticket: Ticket, repo: str) -> str:
    """Stacked-PR target resolver (#940).

    Reads ``ticket.extra['target_branch']`` first (stacked PRs base on
    a different branch than ``origin/main``); falls back to
    ``origin/<default>`` — the same resolver shape
    :func:`assert_commits_ahead_of_base` uses.
    """
    extra = ticket.extra or {}
    explicit = str(extra.get("target_branch") or "").strip()
    if explicit:
        return explicit if "/" in explicit else f"origin/{explicit}"
    try:
        return f"origin/{git.default_branch(repo=repo)}"
    except (CommandFailedError, RuntimeError, ValueError):
        return "origin/main"


def run_branch_currency_gate(
    ticket: Ticket,
    worktree: Worktree,
) -> BranchCurrencyFailure | None:
    """Auto-merge ``target`` into the feature branch before the rest of the gates (#940).

    Placed FIRST in :func:`_run_ship_gates` so the visual-QA gate, the
    phase gate, and the eventual cold-review attestation all run
    against the post-merge tree — not a stale base whose target-branch
    fixes are missing. On zero-conflict the new HEAD is recorded as
    ``ship_invoking_branch``'s post-merge sha so the cold reviewer
    attests the SHA the loop will actually push. On conflict the gate
    refuses cleanly (``git merge --abort`` already restored the tree).
    Fetch failures (offline, auth) are inconclusive — same posture as
    :mod:`teatree.core.gates.clone_guard`.
    """
    repo = (worktree.extra or {}).get("worktree_path", "") or worktree.repo_path
    branch = worktree.branch
    if not repo or not branch:
        return None
    target = resolve_target_branch(ticket, repo)

    result = require_current_branch(repo, branch, target=target)
    if result["error"]:
        return BranchCurrencyFailure(
            allowed=False,
            error=result["error"],
            hint=result["hint"] or "",
            branch=branch,
            target=target,
        )
    post_sha = result["post_merge_sha"]
    if result["auto_merged"] and post_sha:
        # #800 N3: canonical locked RMW — record the post-merge HEAD so
        # the cold reviewer's attestation binds to the tree the loop
        # will actually push, not the pre-merge stale base.
        ticket.merge_extra(set_keys={"branch_currency_post_merge_sha": post_sha})
    return None


def check_shipping_gate(ticket: Ticket) -> ShippingGateFailure | None:
    """Reconcile ``ticket.state`` from the session, or block with missing phases.

    ``Session.visited_phases`` is the single source of truth (#694). When the
    required phases are present this auto-walks the FSM to REVIEWED so
    ``ticket.ship()`` is legal — the gate and ``ticket.state`` can no longer
    disagree. When phases are missing it returns structured JSON with the
    exact ``missing`` list so the calling agent can satisfy the gate rather
    than hitting a raw ``TransitionNotAllowed``.
    """
    from teatree.core.models.errors import QualityGateError  # noqa: PLC0415

    # #801 SSOT: canonical earliest selection (was -pk-latest); the
    # gate only READS — create=False so a gate check never mints a
    # session as a side effect.
    session = ticket.find_phase_session()
    if session is None:
        # No session => no attested work; nothing to reconcile. Returning
        # ``None`` here would let ``ticket.ship()`` raise a raw
        # ``TransitionNotAllowed`` from a non-REVIEWED state, breaking the
        # "pr create never raises a raw TransitionNotAllowed" invariant.
        required = Session._REQUIRED_PHASES.get("shipping", [])  # noqa: SLF001
        return ShippingGateFailure(
            allowed=False,
            error="No session: no attested work to reconcile.",
            missing=list(required),
            hint="Run the work through the loop (or `lifecycle visit-phase`) so the phases are recorded, then retry.",
        )
    try:
        # Single source of truth = the union of phase records across ALL
        # the ticket's sessions, not just the latest (#694). FSM-advancing
        # `visit-phase` forks fresh sessions by design; the required phases
        # are legitimately scattered across the ticket lifecycle.
        session.check_gate_across_ticket("shipping")
    except QualityGateError as exc:
        # #1118: normalize the visited list before comparing — ``_check_phases``
        # normalizes both sides (#782), so an unnormalized comparison here
        # would name a phase as missing that the gate itself accepted.
        visited, _ = ticket.aggregate_phase_records()
        canonical_visited = {normalize_phase(phase) for phase in visited}
        required = Session._REQUIRED_PHASES.get("shipping", [])  # noqa: SLF001
        missing = [p for p in required if p not in canonical_visited]
        return ShippingGateFailure(
            allowed=False,
            error=f"Gate check failed: {exc}",
            missing=missing,
            hint="Spawn a review sub-agent to satisfy the reviewing gate, then retry.",
        )

    # Gate passed -> the work is attested. Reconcile the FSM so ``ship()``
    # is legal and drain any orphan reviewing task (gate-verified only).
    reconcile_fsm_for_ship(ticket, consume_reviewing_tasks=True)
    return None


def resolve_base_url(worktree: Worktree | None) -> str:
    """Return the frontend URL recorded by ``Worktree.verify()``.

    Prefers ``urls['frontend']`` (what users browse) over ``urls['backend']``.
    Falls back to ``http://127.0.0.1:8000`` when no URLs are recorded yet.
    """
    if worktree is None:
        return "http://127.0.0.1:8000"
    urls = worktree.get_extra().get("urls", {})
    return urls.get("frontend") or urls.get("backend") or "http://127.0.0.1:8000"


def run_visual_qa_gate(ticket: Ticket, *, skip_reason: str = "") -> VisualQAGateFailure | None:
    """Run the pre-push browser sanity gate before PR creation.

    Records a JSON summary on ``ticket.extra['visual_qa']`` when the gate
    actually ran (i.e. not skipped for env/flag reasons) so the result
    survives in the FSM history.  Returns an error dict when blocking
    findings are present so the caller can refuse PR creation, or
    ``None`` when the gate passes / is skipped.
    """
    # #776 N1: resolve the INVOKING worktree (same root cause as the
    # ship-branch fix) — a reused multi-workstream ticket must run visual
    # QA against the current workstream's repo, not the stale earliest
    # `worktrees.first()` row. The `ship_invoking_branch` hint is recorded
    # by `create()` before the gates run, so the canonical resolver has
    # the data here too.
    extra = cast("TicketExtra", ticket.extra or {})
    worktree = resolve_ship_worktree(ticket, extra)
    repo_path = worktree.repo_path if worktree else "."
    base_url = resolve_base_url(worktree)

    overlay = get_overlay()
    diff = visual_qa.changed_files(repo=repo_path)
    report = visual_qa.evaluate(diff=diff, overlay=overlay, base_url=base_url, skip_reason=skip_reason)

    # Only persist when the gate produced a meaningful signal — skipping a
    # no-op run keeps the FSM history readable.
    if report.pages or report.has_errors:
        # #800 N3: canonical locked RMW — concurrent pr_urls (ship
        # worker) writer no longer clobbers visual_qa.
        ticket.merge_extra(set_keys={"visual_qa": report.summary()})

    if not report.has_errors:
        return None
    return VisualQAGateFailure(
        allowed=False,
        error=f"Visual QA found {report.total_errors} blocking finding(s).",
        visual_qa=report.summary(),
        report_markdown=visual_qa.format_report(report),
        hint="Fix the findings, or pass --skip-visual-qa <reason> to bypass.",
    )


class E2EMandatoryGateFailure(TypedDict):
    """Pre-ship mandatory-E2E gate refusal (#1967).

    Returned when the change is customer-display-impacting but has no green E2E
    evidence at the reviewed tree and no recorded user bypass. ``error`` names
    both remedies verbatim (the record-e2e-run command and the e2e-bypass
    command).
    """

    allowed: bool
    error: str


def run_e2e_mandatory_gate(ticket: Ticket) -> E2EMandatoryGateFailure | None:
    """Refuse a customer-display-impacting ship without green E2E evidence (#1967).

    Resolves the ship worktree's diff (the same ``origin/main...HEAD`` source
    the visual-QA gate uses) and head SHA, asks the active overlay to classify
    display impact, then runs the mandatory-E2E gate. A recorded user bypass at
    the reviewed tree is consumed single-use here. Returns a structured failure
    naming both remedies on a block, or ``None`` when the gate passes.

    When the worktree path or head SHA cannot be resolved (no real repo, git
    error) the gate cannot bind to a tree — it returns ``None`` (unverifiable,
    not a confirmed block), mirroring the inconclusive posture of
    :func:`assert_commits_ahead_of_base` / the branch-currency gate.
    """
    extra = cast("TicketExtra", ticket.extra or {})
    worktree = resolve_ship_worktree(ticket, extra)
    repo_path = (worktree.worktree_path or worktree.repo_path) if worktree else "."
    try:
        head = git.head_sha(repo=repo_path)
        diff = visual_qa.changed_files(repo=repo_path)
    except (CommandFailedError, RuntimeError, ValueError):
        return None
    if not head:
        return None

    inputs = resolve_gate_inputs(ticket, changed_files=diff, head_sha=head)
    try:
        check_e2e_mandatory(inputs)
    except E2EMandatoryGateError as exc:
        return E2EMandatoryGateFailure(allowed=False, error=str(exc))
    return None
