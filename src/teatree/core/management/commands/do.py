"""``t3 <overlay> do <ticket-ref>`` — the golden-path lifecycle wrapper (PR-31).

One command walks a ticket through the lifecycle (intake -> provision -> plan ->
code -> test -> review -> ship), invoking each phase's EXISTING chokepoint with
its EXISTING gates. It SEQUENCES and REPORTS — it never re-implements a gate or
touches the FSM directly (the ordering + status logic lives in
:mod:`teatree.core.lifecycle_pipeline`).

Resumable + idempotent: it reads ``Ticket.state`` and resumes at the live phase,
never redoing a completed one. An auto step whose gate refuses surfaces the
blocker and stops (exit 1). An agent phase (plan/code/test/review) is reported
``pending`` and the walk stops — dispatching the phase agent is the loop's job
(BLUEPRINT §5.2), not this wrapper's; a re-run resumes once the agent advanced
the FSM. ``--json`` emits the per-step typed status through PR-30's ``emit`` seam
(pure JSON on stdout, human view on stderr); ``--plan`` prints the sequence with
no side effects.

Non-zero exits use ``raise SystemExit`` (never ``typer.Exit``): django-typer
swallows ``typer.Exit`` into a returned code and exits 0, which would report a
blocked golden path as success to a machine front-end.
"""

from typing import IO, Annotated, NotRequired, TypedDict, cast

import typer
from django.core.management import call_command
from django_typer.management import TyperCommand

from teatree.core.lifecycle_pipeline import DoReport, DriveSeams, LifecycleStep, StepReport, TicketSnapshot, drive
from teatree.core.machine_output import emit
from teatree.core.models import Ticket, Worktree
from teatree.core.models.errors import InvalidTransitionError

# stopped_reason values that mean "the operator must act" -> exit 1.
_FAILED_REASONS = frozenset({"blocked", "ignored"})


class _ChokepointError(TypedDict, total=False):
    """The shared error shape ``pr create`` returns on a gate refusal (has ``error``)."""

    error: str


class StepPayload(TypedDict):
    """One step's machine-readable status in the ``--json`` document."""

    name: str
    kind: str
    status: str
    target_state: str
    chokepoint: str
    needs: NotRequired[str]  # agent steps: the phase whose agent must run
    blocker: NotRequired[str]  # BLOCKED steps: the gate refusal message


class DoPayload(TypedDict):
    """The full ``t3 <overlay> do --json`` document a front-end drives off."""

    ticket_ref: str
    ticket_id: int | None
    plan_only: bool
    initial_state: str | None
    final_state: str | None
    stopped_at: str | None
    stopped_reason: str
    steps: list[StepPayload]


def _build_snapshot(ref: str) -> tuple[TicketSnapshot, int | None]:
    """Resolve *ref* to a pure :class:`TicketSnapshot` + pk (absent -> None)."""
    try:
        ticket = Ticket.objects.resolve(ref)
    except Ticket.DoesNotExist:
        return TicketSnapshot(exists=False, state=None, provisioned=False, ignored=False), None
    provisioned = ticket.worktrees.exclude(state=Worktree.State.CREATED).exists()
    snapshot = TicketSnapshot(
        exists=True,
        state=str(ticket.state),
        provisioned=provisioned,
        ignored=ticket.state == Ticket.State.IGNORED,
    )
    return snapshot, int(ticket.pk)


def _chokepoint_argv(step: LifecycleStep, *, ref: str, ticket_id: int | None) -> tuple[str, ...]:
    """The existing chokepoint command each auto step drives through."""
    if step.name == "intake":
        # intake accepts the raw ref (issue URL / number) — the ticket may not exist yet.
        return ("workspace", "ticket", ref)
    if step.name == "provision":
        return ("workspace", "provision", str(ticket_id))
    if step.name == "ship":
        return ("pr", "create", str(ticket_id))
    msg = f"no chokepoint mapping for auto step {step.name!r}"
    raise ValueError(msg)


def _result_error_detail(result: object) -> str:
    """A blocker message from a chokepoint's return, or ``""`` when it succeeded.

    ``pr create`` returns a typed error mapping (with an ``error`` key) on a gate
    refusal rather than raising; a success return carries no ``error`` key.
    """
    if not isinstance(result, dict):
        return ""
    error = cast("_ChokepointError", result).get("error")
    return str(error) if error else ""


def _invoke_chokepoint(step: LifecycleStep, *, ref: str, ticket_id: int | None, err: IO[str]) -> str:
    """Run *step*'s existing chokepoint command; return a blocker detail or ``""``.

    Both of the child command's streams are redirected to *err* so ``do``'s
    stdout stays a pure JSON channel (its own stdout is never handed to a child).
    A gate refusal surfaces either as a raised ``SystemExit``/``InvalidTransitionError``
    (``workspace``/FSM refusals) or a returned error mapping (``pr create``); both
    are normalised to a blocker string. The caller (``drive``) confirms RAN vs
    BLOCKED by re-reading state, so a missed edge still stops rather than
    false-advancing.
    """
    argv = _chokepoint_argv(step, ref=ref, ticket_id=ticket_id)
    try:
        result = call_command(*argv, stdout=err, stderr=err)
    except SystemExit as exc:
        return str(exc.code) if isinstance(exc.code, str) and exc.code else f"the {step.name} chokepoint refused"
    except InvalidTransitionError as exc:
        return str(exc)
    return _result_error_detail(result)


def _step_payload(report: StepReport) -> StepPayload:
    step = report.step
    payload: StepPayload = {
        "name": step.name,
        "kind": step.kind.value,
        "status": report.status.value,
        "target_state": step.target,
        "chokepoint": step.chokepoint,
    }
    if step.needs:
        payload["needs"] = step.needs
    if report.detail:
        payload["blocker"] = report.detail
    return payload


def _payload(report: DoReport) -> DoPayload:
    return {
        "ticket_ref": report.ticket_ref,
        "ticket_id": report.ticket_id,
        "plan_only": report.plan_only,
        "initial_state": report.initial_state,
        "final_state": report.final_state,
        "stopped_at": report.stopped_at,
        "stopped_reason": report.stopped_reason,
        "steps": [_step_payload(step) for step in report.steps],
    }


def _render_human(report: DoReport, stream: IO[str]) -> None:
    header = "PLAN" if report.plan_only else "DO"
    stream.write(f"{header} {report.ticket_ref} ({report.initial_state or 'absent'} -> {report.final_state}):\n")
    for item in report.steps:
        line = f"  {item.step.name:<10} {item.status.value}"
        if item.step.needs and item.status.value == "pending":
            line += f" (needs {item.step.needs})"
        if item.detail:
            line += f" — {item.detail}"
        stream.write(line + "\n")
    stop = report.stopped_at or "—"
    stream.write(f"stopped at: {stop} ({report.stopped_reason})\n")


class Command(TyperCommand):
    def handle(
        self,
        ticket_ref: Annotated[str, typer.Argument(help="Ticket pk, issue number, issue URL, or repo#N.")],
        *,
        plan: Annotated[
            bool,
            typer.Option("--plan", help="Dry-run: print the step sequence it would run, with no side effects."),
        ] = False,
        json_output: Annotated[
            bool,
            typer.Option("--json", help="Emit the per-step typed status as JSON on stdout (human view -> stderr)."),
        ] = False,
    ) -> None:
        """Walk a ticket through the lifecycle, each phase's existing gate enforced."""
        out = cast("IO[str]", self.stdout)
        err = cast("IO[str]", self.stderr)

        seams = DriveSeams(
            snapshot_provider=lambda: _build_snapshot(ticket_ref)[0],
            ticket_id_provider=lambda: _build_snapshot(ticket_ref)[1],
            chokepoint_runner=lambda step: _invoke_chokepoint(
                step, ref=ticket_ref, ticket_id=_build_snapshot(ticket_ref)[1], err=err
            ),
        )
        report = drive(ticket_ref, seams, plan_only=plan)

        self.print_result = False
        emit(
            _payload(report),
            json_output=json_output,
            out=out,
            err=err,
            human=lambda stream: _render_human(report, stream),
        )
        if not report.plan_only and report.stopped_reason in _FAILED_REASONS:
            raise SystemExit(1)
