"""Lifecycle phase-coverage gate at the ``merge_safe`` verdict chokepoint (#3762).

The hole this forecloses: a change can be implemented entirely OUT OF BAND — by
a raw agent session that never enters teatree's lifecycle — and then handed to
teatree as a finished PR with a single instruction, "review this". Every gate
teatree owns is keyed to a phase (the plan-currency gate at ``code()``, the
mandatory-E2E gate at ship/CLEAR, the anti-vacuity attestation, the shipping
gate's required-phase check). A ticket that never enters ``coding`` or
``testing`` reaches merge with all of them structurally absent — not bypassed,
never armed. An out-of-band change ships that way: a file generated from an
in-code dataclass, exported but never read by the resolver, one phase of a
multi-phase plan silently skipped, and its ticket's ledger carried
``visited_phases: ['reviewing']`` and one task, also at ``reviewing``. Across the
whole ledger that is the anomaly — 304 of 306 tickets carry a task or a phase
visit for the work itself.

The chokepoint is deliberately the ``merge_safe`` ``ReviewVerdict``: it is the
one artifact every merge needs (``assert_review_verdict_gate`` refuses a merge
without a non-stale ``merge_safe`` verdict at the live head) and it is the one
door that CANNOT be routed around by skipping a phase, because it is not keyed
to a phase. The gate is enforced inside the guarded
:meth:`~teatree.core.models.review_verdict.ReviewVerdict.record` factory, so it
covers the standalone ``review record`` command AND the ``ticket clear``
by-product verdict with one implementation.

REFUSAL, not an advisory finding, and never-lockout is honoured the way every
sibling domain gate honours it (``e2e_mandatory_gate``, ``merge_quality_gate``,
``rubric_gate``) — by being satisfiable rather than suppressible:

*   a HOLD verdict is NEVER gated, so a reviewer can always record findings;
*   a ticket with no lifecycle ledger at all (a stranger's PR teatree never
    owned) is not gated — there is no lifecycle to route around;
*   only the FIRST ``merge_safe`` verdict for a PR is judged: coverage is a
    ticket-level property, so a re-review at a moved head and the conflict-only
    rebind carry-forward are never re-gated;
*   the natural satisfier is to record the work that happened —
    ``t3 <overlay> lifecycle visit-phase <id> coding``;
*   the sanctioned external-delivery loop records ``e2e`` work of its own, so a
    hand delivery teatree genuinely verified is covered without an override;
*   the escape for genuinely out-of-band work is an explicit, attributable,
    single-use :class:`~teatree.core.models.out_of_band_approval.OutOfBandWorkApproval`
    with a human approver (maker≠checker) and a mandatory reason;
*   ``phase_coverage_gate_enabled`` is the operator's kill-switch (DB-home,
    per-overlay overridable, default on);
*   any unexpected failure resolving the ledger fails OPEN — a crash is never a
    deny.

It lives in ``core.models`` rather than ``core.gates`` because it is enforced
INSIDE a model factory: ``core.gates`` sits above ``core.models`` in the
dependency graph, so a ``core.models`` module importing one would be a cycle
tach refuses. The siblings in ``core.gates`` are the gates called from ABOVE the
models layer (the merge executor, the command layer); this one is the guarded
factory's own contract, next to ``MergeClear``'s.
"""

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from django.core.exceptions import ImproperlyConfigured
from django.db import DatabaseError, transaction

from teatree.core.modelkit.phases import normalize_phase
from teatree.core.models.config_setting import ConfigSetting
from teatree.core.models.errors import InvalidTransitionError
from teatree.core.models.out_of_band_approval import OutOfBandWorkApproval, OutOfBandWorkAudit
from teatree.core.models.pr_ticket_lookup import ticket_for_pr

if TYPE_CHECKING:
    from teatree.core.models.ticket import Ticket

#: The phases in which teatree exercises the CHANGE ITSELF — writing it, running
#: it, or verifying it end to end. Any one of them is enough: a ticket that coded
#: but recorded no separate testing phase is normal, and the sanctioned external
#: delivery loop (`ReviewLoop.start_external_loop`) records `e2e` rather than
#: `coding`. Planning, scoping, reviewing, shipping and retro are deliberately
#: absent — they are phases ABOUT the change, not ON it, so a ticket carrying
#: only those never exercised the work.
WORK_PHASES: frozenset[str] = frozenset({"coding", "testing", "e2e", "debugging"})

#: The DB-home kill-switch key (per-overlay overridable, default on).
GATE_SETTING: str = "phase_coverage_gate_enabled"


class PhaseCoverageError(InvalidTransitionError):
    """A ``merge_safe`` verdict was refused: the work never entered the lifecycle.

    A subclass of :class:`InvalidTransitionError` (sibling of
    ``E2EMandatoryGateError``) so a transition that hits it rolls back and the
    FSM stays put.
    """


@dataclass(frozen=True, slots=True)
class LifecycleCoverage:
    """What a ticket's ledger says about where its work entered the lifecycle."""

    visited_phases: list[str] = field(default_factory=list)
    task_phases: list[str] = field(default_factory=list)

    @property
    def has_lifecycle_record(self) -> bool:
        """True iff the ticket entered teatree's lifecycle at all."""
        return bool(self.visited_phases or self.task_phases)

    @property
    def covered(self) -> bool:
        """True iff a :data:`WORK_PHASES` phase visit or task exists."""
        return bool(WORK_PHASES & (set(self.visited_phases) | set(self.task_phases)))

    def entered_at(self) -> str:
        """The comma-joined phases the work is recorded at, for the refusal message."""
        seen: list[str] = []
        for phase in [*self.visited_phases, *self.task_phases]:
            if phase not in seen:
                seen.append(phase)
        return ", ".join(f"'{phase}'" for phase in seen) or "'(nothing)'"


def lifecycle_coverage(ticket: "Ticket") -> LifecycleCoverage:
    """Read *ticket*'s phase-visit union and task ledger, canonicalised.

    Both halves are normalised through :func:`normalize_phase`, so a session that
    stored the short verb ``code`` and a task stored as ``coding`` resolve to the
    same canonical token and neither spelling can read as absent.
    """
    visited, _ = ticket.aggregate_phase_records()
    task_phases = ticket.tasks.exclude(phase="").values_list("phase", flat=True)
    return LifecycleCoverage(
        visited_phases=_canonical(visited),
        task_phases=_canonical(task_phases),
    )


def _canonical(phases: Iterable[str]) -> list[str]:
    """De-duplicated canonical tokens, order preserved."""
    seen: list[str] = []
    for raw in phases:
        phase = normalize_phase(str(raw))
        if phase and phase not in seen:
            seen.append(phase)
    return seen


def gate_enabled(overlay: str) -> bool:
    """Resolve the gate's OWN kill-switch: overlay scope, then global, then ``True``.

    Read straight from the ``ConfigSetting`` DB store (the canonical override
    tier) rather than the typed settings dataclass, so the switch resolves
    identically in the env-less merge keystone and the interactive CLI.
    """
    scopes = [overlay.strip(), ""] if overlay.strip() else [""]
    for scope in scopes:
        value = ConfigSetting.objects.get_effective(GATE_SETTING, scope)
        if value is not None:
            return bool(value)
    return True


def _work_phase_list() -> str:
    return " / ".join(sorted(WORK_PHASES))


def _deny_message(ticket: "Ticket", coverage: LifecycleCoverage, head_sha: str) -> str:
    return (
        f"Refusing to record a merge_safe verdict for ticket {ticket.pk} at {head_sha[:8]}: its lifecycle "
        f"ledger shows the work entered only at {coverage.entered_at()} — no {_work_phase_list()} phase "
        f"visit or task exists for it. That is the shape of work implemented OUT OF BAND and handed to "
        f"teatree as a finished PR to review, which leaves every phase-keyed gate (plan currency, "
        f"mandatory E2E, anti-vacuity, the shipping gate) structurally absent rather than satisfied "
        f"(#3762). Satisfy the gate with EITHER:\n"
        f"  1. record the work that actually happened:  t3 <overlay> lifecycle visit-phase {ticket.pk} coding "
        f"(and `testing`)\n"
        f"  2. OR, for legitimately out-of-band work (a docs typo, a revert, a dependency bump), an "
        f"explicit human override:  t3 <overlay> lifecycle approve-out-of-band {ticket.pk} "
        f"--approver <user-id> --head-sha {head_sha} --reason '<why this skipped the lifecycle>'\n"
        f"The override is single-use, bound to this tree, and refuses a maker/coding-agent/loop approver — "
        f"an unattributed bypass is the hole this closes. The operator kill-switch is "
        f"`t3 <overlay> config_setting set {GATE_SETTING} false`."
    )


def _coverage_to_judge(ticket: "Ticket") -> LifecycleCoverage | None:
    """The coverage this ticket must answer for, or ``None`` when it is not gated.

    ``None`` covers every pass-without-an-override case: the kill-switch is off,
    the ledger carries coding/testing coverage, the ticket never entered the
    lifecycle at all — and the never-lockout case, a DB or app-registry failure
    while reading either. A gate that cannot read its own evidence must fail
    OPEN; a crash is never a deny.
    """
    try:
        if not gate_enabled(ticket.overlay or ""):
            return None
        coverage = lifecycle_coverage(ticket)
    except (DatabaseError, ImproperlyConfigured):
        return None
    if coverage.covered or not coverage.has_lifecycle_record:
        return None
    return coverage


def check_phase_coverage(ticket: "Ticket", *, head_sha: str) -> None:
    """Refuse a ``merge_safe`` verdict for a ticket whose work skipped the lifecycle.

    Passes silently whenever :func:`_coverage_to_judge` reports the ticket is not
    gated. When the only satisfier is a recorded override it is consumed
    single-use inside one ``transaction.atomic`` block together with the audit
    write, so a concurrent second evaluation cannot reuse it. Raises
    :class:`PhaseCoverageError` naming both remedies otherwise.
    """
    coverage = _coverage_to_judge(ticket)
    if coverage is None:
        return

    with transaction.atomic():
        consumed = OutOfBandWorkApproval.consume(ticket, head_sha)
        if consumed is None:
            raise PhaseCoverageError(_deny_message(ticket, coverage, head_sha.strip()))
        OutOfBandWorkAudit.objects.create(
            approval=consumed,
            ticket=ticket,
            head_sha=consumed.head_sha,
            approver_id=consumed.approver_id,
            reason=consumed.reason,
        )


def assert_phase_coverage_for_verdict(*, ticket: "Ticket | None", slug: str, pr_id: int, head_sha: str) -> None:
    """The ``ReviewVerdict.record`` entry point — resolve the ticket, then gate.

    ``ticket`` is the explicitly-passed one when the caller supplied it; otherwise
    it is resolved from ``(slug, pr_id)`` via the PR ledger / CLEAR, so omitting
    ``--ticket-id`` on ``review record`` cannot dodge the gate. A genuinely
    unresolvable ticket is a no-op — the PR belongs to no lifecycle teatree owns.
    """
    resolved = ticket if ticket is not None else ticket_for_pr(slug=slug, pr_id=pr_id)
    if resolved is None:
        return
    check_phase_coverage(resolved, head_sha=head_sha)
