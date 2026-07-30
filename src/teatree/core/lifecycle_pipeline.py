"""The golden-path lifecycle sequencer behind ``t3 <overlay> do`` (PR-31).

``t3 <overlay> do <ticket-ref>`` is a thin front-end-seam wrapper: it walks a
ticket through the lifecycle (intake -> provision -> plan -> code -> test ->
review -> ship) by invoking each phase's EXISTING chokepoint command with its
EXISTING gates. It SEQUENCES and REPORTS — no new gate logic, no FSM change.

This module is that sequencing logic, kept pure and side-effect-free so it is
the single source of truth for the step order, the ``Ticket.state`` -> status
mapping (resumability), and the ``--plan`` dry-run. :func:`drive` executes the
walk against injected seams — a snapshot provider and a chokepoint runner — so
``do`` (the command) wires the real ``call_command`` chokepoints while tests
drive the sequencing with fakes.

Two step kinds split the lifecycle by who can advance it. ``AUTO`` steps
(``workspace ticket`` / ``workspace provision`` / ``pr create``) are deterministic
chokepoints the wrapper invokes itself, and a gate refusal on one surfaces as
``BLOCKED``. ``AGENT`` steps (plan/code/test/review) need the phase agent, so the
wrapper reports ``PENDING`` and stops — dispatching the agent is the loop's job
(BLUEPRINT §5.2), never this wrapper's; a re-run resumes at the new live phase
once the agent advanced the FSM.
"""

import dataclasses
import enum
from collections.abc import Callable
from itertools import starmap

from teatree.core.models import Ticket

# The linear position of each state on the golden path. A step is DONE once the
# ticket has reached (>=) the step's target position, so resumability is a pure
# comparison against ``Ticket.state``. IGNORED is off-path (handled via the
# snapshot's ``ignored`` flag), so it is intentionally absent here.
_STATE_ORDER: dict[str, int] = {
    Ticket.State.NOT_STARTED: 0,
    Ticket.State.SCOPED: 1,
    Ticket.State.STARTED: 2,
    Ticket.State.PLANNED: 3,
    Ticket.State.CODED: 4,
    Ticket.State.TESTED: 5,
    Ticket.State.REVIEWED: 6,
    Ticket.State.SHIPPED: 7,
    Ticket.State.IN_REVIEW: 8,
    Ticket.State.MERGED: 9,
    Ticket.State.RETROSPECTED: 10,
    Ticket.State.DELIVERED: 11,
}
_ABSENT_ORDER = -1  # the ticket does not exist yet (intake has not run)
_STARTED_ORDER = _STATE_ORDER[Ticket.State.STARTED]


class StepKind(enum.Enum):
    AUTO = "auto"  # a deterministic chokepoint ``do`` invokes itself
    AGENT = "agent"  # a phase whose work needs the phase agent (do reports, never runs it)


class StepStatus(enum.Enum):
    DONE = "done"  # the ticket is already at/past this step's target (skipped)
    RUN = "run"  # dry-run only: the current auto step would run now
    RAN = "ran"  # executed this invocation and advanced the FSM
    BLOCKED = "blocked"  # the current auto step's gate refused
    PENDING = "pending"  # the current agent step — needs the phase agent
    WAITING = "waiting"  # a later step, its upstream not complete


@dataclasses.dataclass(frozen=True)
class LifecycleStep:
    name: str
    kind: StepKind
    target: str  # the ``Ticket.State`` this step reaches
    chokepoint: str  # human description of the command / agent that performs it
    needs: str = ""  # agent steps: the phase whose agent must run before it advances

    @property
    def target_order(self) -> int:
        return _STATE_ORDER[self.target]


#: The golden path — the single source of truth for the step order and each
#: step's target state. intake and provision both land at ``STARTED`` (``workspace
#: ticket`` provisions synchronously); provision earns its own step because a
#: failed provision leaves worktrees ``CREATED`` and is separately retriable.
PIPELINE: tuple[LifecycleStep, ...] = (
    LifecycleStep("intake", StepKind.AUTO, Ticket.State.STARTED, "workspace ticket <ref>"),
    LifecycleStep("provision", StepKind.AUTO, Ticket.State.STARTED, "workspace provision"),
    LifecycleStep("plan", StepKind.AGENT, Ticket.State.PLANNED, "planner agent", needs="planning"),
    LifecycleStep("code", StepKind.AGENT, Ticket.State.CODED, "coder agent", needs="coding"),
    LifecycleStep("test", StepKind.AGENT, Ticket.State.TESTED, "tester agent", needs="testing"),
    LifecycleStep("review", StepKind.AGENT, Ticket.State.REVIEWED, "reviewer agent", needs="reviewing"),
    LifecycleStep("ship", StepKind.AUTO, Ticket.State.SHIPPED, "pr create <id>"),
)


@dataclasses.dataclass(frozen=True)
class TicketSnapshot:
    """The minimal, pure view of a ticket the pipeline scores against."""

    exists: bool
    state: str | None  # ``Ticket.state``, or None when the ticket is absent
    provisioned: bool  # >=1 worktree past CREATED
    ignored: bool  # state == IGNORED (off-path terminal)

    @property
    def order(self) -> int:
        if not self.exists or self.state is None:
            return _ABSENT_ORDER
        return _STATE_ORDER.get(self.state, _ABSENT_ORDER)


@dataclasses.dataclass(frozen=True)
class StepReport:
    step: LifecycleStep
    status: StepStatus
    detail: str = ""  # the blocker message for BLOCKED; empty otherwise


@dataclasses.dataclass(frozen=True)
class DoReport:
    ticket_ref: str
    initial_state: str | None
    final_state: str | None
    ticket_id: int | None
    steps: list[StepReport]
    stopped_at: str | None  # the step where the walk stopped, None when it completed
    stopped_reason: str  # completed | pending | blocked | ignored | runnable (dry-run)
    plan_only: bool


def _step_done(step: LifecycleStep, snapshot: TicketSnapshot) -> bool:
    """Whether *step*'s target has already been reached (so ``do`` skips it)."""
    order = snapshot.order
    if step.name == "provision":
        # intake and provision share the STARTED target; provision is done only
        # once worktrees are actually provisioned (or the ticket is past STARTED).
        return order > _STARTED_ORDER or (order == _STARTED_ORDER and snapshot.provisioned)
    return order >= step.target_order


def resolve_plan(snapshot: TicketSnapshot) -> list[tuple[LifecycleStep, StepStatus]]:
    """The per-step planned status for ``--plan`` — purely from the snapshot.

    Every done step is ``DONE``; the first not-done step is the current one
    (``RUN`` for an auto step, ``PENDING`` for an agent step); everything after
    it is ``WAITING`` (its upstream is not complete).
    """
    result: list[tuple[LifecycleStep, StepStatus]] = []
    current_found = False
    for step in PIPELINE:
        if _step_done(step, snapshot):
            result.append((step, StepStatus.DONE))
        elif not current_found:
            current_found = True
            status = StepStatus.RUN if step.kind is StepKind.AUTO else StepStatus.PENDING
            result.append((step, status))
        else:
            result.append((step, StepStatus.WAITING))
    return result


def _plan_stop(reports: list[StepReport]) -> tuple[str | None, str]:
    for report in reports:
        if report.status is StepStatus.RUN:
            return report.step.name, "runnable"
        if report.status is StepStatus.PENDING:
            return report.step.name, "pending"
    return None, "completed"


def _ignored_report(ref: str, snapshot: TicketSnapshot, ticket_id: int | None, *, plan_only: bool) -> DoReport:
    return DoReport(
        ticket_ref=ref,
        initial_state=snapshot.state,
        final_state=snapshot.state,
        ticket_id=ticket_id,
        steps=[StepReport(step, StepStatus.WAITING) for step in PIPELINE],
        stopped_at=None,
        stopped_reason="ignored",
        plan_only=plan_only,
    )


@dataclasses.dataclass(frozen=True)
class DriveSeams:
    """The injected boundary :func:`drive` walks against — DB read + command invoke.

    ``snapshot_provider`` re-reads the live ``Ticket.state`` (called after each
    executed auto step so the walk resumes). ``chokepoint_runner`` runs an auto
    step's existing command and returns a blocker message (``""`` when it ran
    without surfacing an error). Keeping them behind one object lets the command
    wire the real ``call_command`` seams while tests inject fakes.
    """

    snapshot_provider: Callable[[], TicketSnapshot]
    ticket_id_provider: Callable[[], int | None]
    chokepoint_runner: Callable[[LifecycleStep], str]


def _execute_auto_step(step: LifecycleStep, seams: DriveSeams) -> StepReport:
    """Run an auto step's chokepoint; RAN if the FSM advanced, else BLOCKED.

    The walk decides RAN vs BLOCKED by re-reading the snapshot rather than
    trusting the runner's return, so both raise-style and return-error-style
    commands (``workspace``/FSM raises vs ``pr create`` error mappings) are
    handled uniformly — a missed edge still stops rather than false-advances.
    """
    detail = seams.chokepoint_runner(step)
    if _step_done(step, seams.snapshot_provider()):
        return StepReport(step, StepStatus.RAN)
    blocker = detail or f"{step.chokepoint} did not advance the ticket to {step.target}"
    return StepReport(step, StepStatus.BLOCKED, blocker)


def _walk(seams: DriveSeams) -> tuple[list[StepReport], str | None, str]:
    """The real-run walk: execute auto steps, stop at the first agent/blocked step."""
    reports: list[StepReport] = []
    stopped_at: str | None = None
    reason = "completed"
    stopped = False
    for step in PIPELINE:
        if stopped:
            reports.append(StepReport(step, StepStatus.WAITING))
            continue
        if _step_done(step, seams.snapshot_provider()):
            report = StepReport(step, StepStatus.DONE)
        elif step.kind is StepKind.AGENT:
            report = StepReport(step, StepStatus.PENDING)
            stopped_at, reason, stopped = step.name, "pending", True
        else:
            report = _execute_auto_step(step, seams)
            if report.status is StepStatus.BLOCKED:
                stopped_at, reason, stopped = step.name, "blocked", True
        reports.append(report)
    return reports, stopped_at, reason


def drive(ref: str, seams: DriveSeams, *, plan_only: bool) -> DoReport:
    """Walk the pipeline against *seams* and return a full per-step report."""
    initial = seams.snapshot_provider()
    ticket_id = seams.ticket_id_provider()
    if initial.ignored:
        return _ignored_report(ref, initial, ticket_id, plan_only=plan_only)
    if plan_only:
        reports = list(starmap(StepReport, resolve_plan(initial)))
        stopped_at, reason = _plan_stop(reports)
        return DoReport(ref, initial.state, initial.state, ticket_id, reports, stopped_at, reason, plan_only=True)
    reports, stopped_at, reason = _walk(seams)
    final = seams.snapshot_provider()
    return DoReport(
        ref, initial.state, final.state, seams.ticket_id_provider(), reports, stopped_at, reason, plan_only=False
    )


__all__ = [
    "PIPELINE",
    "DoReport",
    "DriveSeams",
    "LifecycleStep",
    "StepKind",
    "StepReport",
    "StepStatus",
    "TicketSnapshot",
    "drive",
    "resolve_plan",
]
