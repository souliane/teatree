import logging
import os
import pathlib
import shutil
import sys
from typing import IO, Annotated, cast

import typer
from django_typer.management import TyperCommand, command

from teatree.agents._headless_options import UUID_RE
from teatree.agents.prompt import build_interactive_context
from teatree.agents.skill_bundle import resolve_skill_bundle
from teatree.core.intake.ticket_kind_classification import classify_ticket_kind
from teatree.core.machine_output import emit
from teatree.core.management.commands.tasks_session_view import (
    TaskRow,
    render_reconcile_checklist,
    render_session_view,
    render_tasks_table,
)
from teatree.core.models import InvalidTransitionError, Task, TaskAttempt, Ticket
from teatree.core.models.ticket_worktree_checks import dispatch_worktree_path
from teatree.core.overlay_loader import get_overlay_for_ticket
from teatree.core.session_identity import current_session_id

logger = logging.getLogger(__name__)


class Command(TyperCommand):
    @command()
    # ast-grep-ignore: ac-django-no-complexity-suppressions
    def create(  # noqa: PLR0913 — django-typer command: every param maps 1:1 to a CLI flag (ticket/--phase/--reason/--reason-file/--interactive/--kind); the arg list IS the public `tasks create` surface, not an internal design smell.
        self,
        ticket: Annotated[int, typer.Argument(help="Ticket PK (see `ticket_id` in `tasks list`).")],
        *,
        phase: Annotated[
            str,
            typer.Option(help="Phase: scoping, coding, testing, reviewing, shipping."),
        ] = "",
        reason: Annotated[
            str,
            typer.Option(help="Prompt body for the worker. Use '-' to read from stdin. Overrides --reason-file."),
        ] = "",
        reason_file: Annotated[
            pathlib.Path | None,
            typer.Option(help="Read the prompt body from a file."),
        ] = None,
        interactive: Annotated[
            bool,
            typer.Option(help="Create an interactive task instead of the default headless one."),
        ] = False,
        kind: Annotated[
            str,
            typer.Option(help="Classify the ticket as 'fix' or 'feature' (records Ticket.kind, #17)."),
        ] = "",
    ) -> dict[str, int | str]:
        """Enqueue the next-phase task for a ticket.

        Used by `/t3:next` to hand off from one phase to the next. Headless by default so a worker
        claims it immediately; pass `--interactive` for tasks that require human input. A machine
        handoff: the created-task record is JSON on stdout, the human confirmation on stderr.

        ``--kind`` (#17) records the ticket's FEATURE/FIX classification, arming the S2
        defect-escape signal and the fix-record DoD gate for correction work.
        """
        if not phase.strip():
            self.stderr.write("--phase is required (scoping, coding, testing, reviewing, or shipping).")
            raise SystemExit(1)
        body = _resolve_reason(reason=reason, reason_file=reason_file)
        if not body.strip():
            self.stderr.write(
                "--reason (or --reason-file, or stdin via '--reason -') is required and must not be blank."
            )
            raise SystemExit(1)

        try:
            ticket_obj = Ticket.objects.get(pk=ticket)
        except Ticket.DoesNotExist:
            self.stderr.write(f"Ticket {ticket} not found.")
            raise SystemExit(1) from None

        if kind.strip():
            try:
                ticket_obj.kind = classify_ticket_kind(explicit=kind)
            except ValueError as exc:
                self.stderr.write(str(exc))
                raise SystemExit(1) from None
            ticket_obj.save(update_fields=["kind"])

        # #801 SSOT: canonical earliest+locked policy (was -pk-latest
        # else an unlocked raw create); non-blank agent_id on miss.
        session = ticket_obj.resolve_phase_session(agent_id="phase-handoff")
        requested = Task.ExecutionTarget.INTERACTIVE if interactive else Task.ExecutionTarget.HEADLESS
        task = Task.objects.create(
            ticket=ticket_obj,
            session=session,
            phase=phase,
            execution_target=requested,
            execution_reason=body,
        )
        # ``Task.save`` routes a loop-dispatched phase to INTERACTIVE regardless
        # of ``--interactive``, so report the persisted target, not the request.
        target = task.execution_target
        payload: dict[str, int | str] = {
            "task_id": task.pk,
            "ticket_id": ticket_obj.pk,
            "phase": phase,
            "execution_target": target,
        }
        self.print_result = False
        self.stderr.write(f"Created task {task.pk} (ticket {ticket_obj.pk}, phase={phase}, target={target}).")
        emit(payload, json_output=True, out=cast("IO[str]", self.stdout), err=cast("IO[str]", self.stderr))
        return payload

    @command()
    def cancel(
        self,
        task_id: int,
        *,
        confirm: bool = False,
        reason: Annotated[
            str,
            typer.Option(help="Audit-trail reason recorded on a TaskAttempt (e.g. 'superseded by !6219')."),
        ] = "",
    ) -> None:
        """Cancel a pending or (with --confirm) claimed task, driving it to FAILED.

        An optional ``--reason`` persists to the DB as a ``TaskAttempt`` (mirroring
        ``complete --note``) so the audit trail records WHY the task was cancelled
        — the cancel transition is otherwise indistinguishable from any other
        failure (#2559). A blank/whitespace reason records no attempt (no empty
        audit row); the cancellation itself is unchanged.
        """
        from django.db import transaction  # noqa: PLC0415 — deferred: Django import at call time
        from django.utils import timezone  # noqa: PLC0415 — deferred: Django import at call time

        with transaction.atomic():
            try:
                task = Task.objects.select_for_update().get(pk=task_id)
            except Task.DoesNotExist:
                self.stderr.write(f"Task {task_id} not found.")
                raise SystemExit(1) from None

            if task.status == Task.Status.CLAIMED and not confirm:
                self.stderr.write(f"Task {task_id} is currently claimed. Pass --confirm to cancel it.")
                raise SystemExit(1)

            if task.status in Task.Status.terminal():
                self.stderr.write(f"Task {task_id} already finished ({task.status}).")
                raise SystemExit(1)

            if reason.strip():
                TaskAttempt.objects.create(
                    task=task,
                    execution_target=task.execution_target,
                    ended_at=timezone.now(),
                    exit_code=1,
                    error=reason.strip(),
                    result={"cancel_reason": reason.strip()},
                )
            task.fail()
        self.stdout.write(f"Task {task_id} cancelled.")

    @command()
    def complete(
        self,
        task_id: Annotated[int, typer.Argument(help="Task ID (see `task_id` in `tasks list`).")],
        *,
        note: Annotated[
            str,
            typer.Option(help="Audit-trail reason recorded on a TaskAttempt (e.g. 'work landed via !6219')."),
        ] = "",
    ) -> None:
        """Mark a claimed or failed task COMPLETED for work finished out-of-band.

        Drives the Task FSM ``claimed → completed`` (releasing the lease and
        auto-advancing the ticket). Idempotent: completing an already-completed
        task is a no-op with exit 0.

        A ``failed`` task whose work later landed out-of-band is resolved the same
        way (``failed → completed``), but ONLY with a mandatory evidence ``--note``
        — the pointer to where that work landed (#1949). Without it there is no
        record of why a failed task was marked done. A ``pending`` task is rejected.

        Fail-closed evidence gate (#1280): when ``--note`` ASSERTS an external
        outcome (merged / posted / shipped / deployed) it must also carry a
        resolvable artifact pointer (URL / SHA / ``!123`` / ``#123`` / note id /
        path / Slack ts), so a phantom "done" claim cannot be recorded without
        proof. A Slack post recorded as ``slack:<channel>:<ts>`` or
        ``<channel>:<ts>`` is normalized to its archives permalink before the gate
        and before storage. A note with no outcome claim — or no note — is
        untouched.
        """
        from django.db import transaction  # noqa: PLC0415 — deferred: Django import at call time
        from django.utils import timezone  # noqa: PLC0415 — deferred: Django import at call time

        from teatree.core.review.completion_evidence import (  # noqa: PLC0415 — deferred: keeps command import light
            CompletionEvidenceError,
            check_completion_evidence,
            normalize_artifact_pointers,
        )

        note = normalize_artifact_pointers(note)
        try:
            check_completion_evidence(note)
        except CompletionEvidenceError as exc:
            self.stderr.write(str(exc))
            raise SystemExit(1) from None

        with transaction.atomic():
            try:
                task = Task.objects.select_for_update().get(pk=task_id)
            except Task.DoesNotExist:
                self.stderr.write(f"Task {task_id} not found.")
                raise SystemExit(1) from None

            if task.status == Task.Status.COMPLETED:
                self.stdout.write(f"Task {task_id} already completed; nothing to do.")
                return

            if task.status == Task.Status.FAILED and not note.strip():
                self.stderr.write(
                    f"Task {task_id} is 'failed'. Completing it out-of-band requires a mandatory evidence "
                    "--note pointing at where the work landed (e.g. --note 'merged via <url-or-!id-or-sha>').",
                )
                raise SystemExit(1)

            if task.status not in {Task.Status.CLAIMED, Task.Status.FAILED}:
                self.stderr.write(
                    f"Task {task_id} is '{task.status}', not 'claimed' or 'failed'. "
                    "Only a claimed or failed task can be completed.",
                )
                raise SystemExit(1)

            if note.strip():
                TaskAttempt.objects.create(
                    task=task,
                    execution_target=task.execution_target,
                    ended_at=timezone.now(),
                    exit_code=0,
                    result={"complete_note": note},
                )
            # Decouple the completion bookkeeping from the FSM auto-advance
            # (#1977): a deliberate gate refusal (no PlanArtifact, dirty
            # worktree, missing shipping attestation) must complete the task —
            # the out-of-band-done write the operator asked for — and SURFACE
            # the refusal loudly, never crash rc=1 and wedge the task claimed.
            ticket_id = task.ticket_id
            advance_failure = task.complete_surfacing_advance_failure()

        self.stdout.write(f"Task {task_id} completed.")
        if advance_failure:
            self.stderr.write(
                f"WARNING: task {task_id} completed but the ticket FSM did NOT advance: {advance_failure}\n"
                f"  The completion stands; record the missing plan ("
                f'`t3 <overlay> ticket plan {ticket_id} "<text>"` or `ticket plan-bypass`) '
                f"and the replay sweep advances the ticket. The task is NOT wedged claimed.",
            )

    @command(name="record-attempt")
    def record_attempt(
        self,
        task_id: Annotated[int, typer.Argument(help="Task ID the in-session sub-agent ran.")],
        result_json: Annotated[
            str,
            typer.Argument(help="The agent result envelope as JSON. Use '-' to read from stdin."),
        ],
        *,
        agent_session_id: Annotated[
            str,
            typer.Option(help="Claude session id of the sub-agent, for resume context on follow-ups."),
        ] = "",
    ) -> None:
        """Record an in-session sub-agent's result back onto a Task (#loop INTERACTIVE path).

        The ``/loop`` slot calls this after its ``Agent`` sub-agent returns: it
        hands the same structured result envelope ``run_headless`` would have
        parsed out of the detached headless-SDK run, and this drives the Task to its
        terminal state through the SHARED recorder — schema-key check, the
        #1284 phase-evidence gate, then ``complete`` (auto-advancing the
        ticket) or ``fail``. Pairs with ``t3 loop claim-next`` /
        ``loop_dispatch spawn-claim``: claim → spawn → record-attempt. The task
        must be ``claimed`` (the claim is the spawn boundary); recording onto a
        finished task is rejected.
        """
        from teatree.agents.attempt_recorder import (  # noqa: PLC0415 — deferred: keeps command import light
            AttemptUsage,
            ResultEnvelopeError,
            parse_result_envelope,
            record_result_envelope,
        )

        payload = sys.stdin.read() if result_json == "-" else result_json
        try:
            result = parse_result_envelope(payload)
        except ResultEnvelopeError as exc:
            self.stderr.write(str(exc))
            raise SystemExit(1) from None

        try:
            task = Task.objects.get(pk=task_id)
        except Task.DoesNotExist:
            self.stderr.write(f"Task {task_id} not found.")
            raise SystemExit(1) from None

        if task.status in Task.Status.terminal():
            self.stderr.write(f"Task {task_id} is already '{task.status}'; cannot record an attempt.")
            raise SystemExit(1)
        if task.status != Task.Status.CLAIMED:
            self.stderr.write(
                f"Task {task_id} is '{task.status}', not 'claimed'. Claim it first "
                "(`t3 loop claim-next` / `loop_dispatch spawn-claim`) before recording its attempt.",
            )
            raise SystemExit(1)

        # souliane/teatree#657: an in-session sub-agent runs inside the user's
        # own Claude Code session — always the Max subscription seat, never
        # metered (see ``TaskAttemptQuerySet.headless()``'s billing contract).
        usage = AttemptUsage(agent_session_id=agent_session_id, lane=TaskAttempt.Lane.SUBSCRIPTION)
        attempt = record_result_envelope(task, result, usage=usage)
        task.refresh_from_db()
        self.stdout.write(f"Recorded attempt {attempt.pk} for task {task_id} (task now '{task.status}').")

    @command(name="list")
    def list_tasks(
        self,
        *,
        status: Annotated[str | None, typer.Option(help="Filter by status")] = None,
        execution_target: Annotated[str | None, typer.Option(help="Filter by execution target")] = None,
        session: Annotated[
            bool,
            typer.Option(help="Scope to the current harness session and group pending / claimed / done."),
        ] = False,
        json_output: Annotated[
            bool,
            typer.Option("--json", help="Emit the task rows as JSON on stdout instead of the human table."),
        ] = False,
    ) -> list[TaskRow]:
        """List the teatree tasks queue (not your harness TODO list).

        A pure READ: it never reaps or reclaims. Failing a stale CLAIMED task from
        a read path (a bare ``reap_stale_claims`` with no preceding
        ``reclaim_orphaned_claims``) would terminally FAIL a recoverable
        crashed-session task on a mere ``tasks list``, bypassing the
        rescue-before-fail ordering the boot/tick ``run_boot_sweeps`` owns.
        """
        if session:
            return self._list_session_todos(status=status, execution_target=execution_target, json_output=json_output)
        qs = Task.objects.select_related("ticket").order_by("pk")
        if status:
            qs = qs.filter(status=status)
        if execution_target:
            qs = qs.filter(execution_target=execution_target)
        rows = [_task_row(task) for task in qs]
        self.print_result = False
        emit(
            rows,
            json_output=json_output,
            out=cast("IO[str]", self.stdout),
            err=cast("IO[str]", self.stderr),
            human=lambda stream: render_tasks_table(rows, stream=stream),
        )
        return rows

    def _list_session_todos(
        self,
        *,
        status: str | None,
        execution_target: str | None,
        json_output: bool,
    ) -> list[TaskRow]:
        """Print the current session's teatree tasks, grouped by status.

        Renders only the teatree ``Task`` rows (DB-backed lifecycle tasks scoped
        to the active harness session via ``Session.agent_id``). It does NOT
        render the harness TODO list: that list is the agent's live in-memory
        ``TaskCreate`` / ``TaskUpdate`` state, which a CLI subprocess cannot read
        (it can only see a stale on-disk snapshot that lags the live session).
        ``/t3:todos`` builds the harness half from the live ``TaskList`` harness
        tool instead, so this view never masquerades as the live session list.
        """
        session_id = current_session_id()
        qs = Task.objects.for_claude_session(session_id).select_related("ticket")
        if status:
            qs = qs.filter(status=status)
        if execution_target:
            qs = qs.filter(execution_target=execution_target)
        rows = [_task_row(task) for task in qs]
        self.print_result = False
        emit(
            rows,
            json_output=json_output,
            out=cast("IO[str]", self.stdout),
            err=cast("IO[str]", self.stderr),
            human=lambda stream: render_session_view(rows, session_id=session_id, stream=stream),
        )
        return rows

    @command(name="reconcile-checklist")
    def reconcile_checklist(self) -> None:
        """Emit the in-session harness-TODO reconciliation checklist (read-only).

        The harness TODO list lives only in the agent's live, in-memory
        ``TaskList`` state — the Task tools bypass ``PreToolUse`` /
        ``PostToolUse`` hooks, so a CLI subprocess (and any background loop)
        can neither read nor write it. Only the in-session agent holding those
        tools can. This command is therefore NOT the maintainer (it cannot
        touch the live list); it is the deterministic *checklist emitter* the
        agent applies with its OWN ``TaskList`` / ``TaskUpdate`` /
        ``TaskCreate`` tools each turn: reconcile the live list against the
        conversation, consolidate/dedupe, and mark completed items done.

        It also surfaces this session's open teatree ``Task`` rows as
        completion anchors (work the loop tracked that the agent may need to
        mark done). It makes NO writes of any kind — it never creates,
        completes, transitions, reaps, or reclaims a task (the live harness list
        is unreachable from a subprocess, and a read surface must not fail a
        recoverable crashed-session task by reaping it without the
        rescue-before-fail reclaim the boot/tick ``run_boot_sweeps`` owns).
        Running it twice prints the same thing.
        """
        session_id = current_session_id()
        qs = (
            Task.objects.for_claude_session(session_id).filter(status__in=Task.Status.active()).select_related("ticket")
        )
        rows = [_task_row(task) for task in qs]
        render_reconcile_checklist(
            rows,
            session_id=session_id,
            stream=cast("IO[str]", self.stdout),
        )

    @command()
    def claim(self, execution_target: str = "headless", claimed_by: str = "worker") -> int | None:
        task = self._claim_next_task(execution_target=execution_target, claimed_by=claimed_by)
        return int(task.pk) if task else None

    @command()
    def work_next_headless(self, claimed_by: str = "worker") -> dict[str, str] | None:
        task = self._claim_next_task(execution_target=Task.ExecutionTarget.HEADLESS, claimed_by=claimed_by)
        if task is None:
            return None
        return self._execute_headless(task)

    @command()
    def start(
        self,
        task_id: Annotated[int, typer.Argument(help="Task ID; omit to start the next pending interactive task.")] = 0,
        claimed_by: Annotated[str, typer.Option(help="Worker identifier stored on the claim.")] = "cli",
    ) -> None:
        """Claim an interactive task and exec ``claude`` in the current terminal."""
        task = self._resolve_interactive_task(task_id=task_id, claimed_by=claimed_by)
        if task is None:
            self.stdout.write("No interactive tasks pending.")
            return

        command_argv = _build_claude_command(task)
        TaskAttempt.objects.create(task=task, execution_target=task.execution_target, launch_url="")

        self.stdout.write(f"Starting task {task.pk} (ticket {task.ticket.ticket_number}) in the current terminal…")
        _exec_inline(command_argv)

    def _resolve_interactive_task(self, *, task_id: int, claimed_by: str) -> Task | None:
        if task_id:
            try:
                task = Task.objects.get(pk=task_id)
            except Task.DoesNotExist:
                self.stderr.write(f"Task {task_id} not found.")
                raise SystemExit(1) from None

            if task.execution_target != Task.ExecutionTarget.INTERACTIVE:
                self.stderr.write(f"Task {task_id} is not an interactive task.")
                raise SystemExit(1)

            try:
                task.claim(claimed_by=claimed_by)
            except InvalidTransitionError as exc:
                self.stderr.write(f"Cannot claim task {task_id}: {exc}")
                raise SystemExit(1) from None
            return task

        return self._claim_next_task(execution_target=Task.ExecutionTarget.INTERACTIVE, claimed_by=claimed_by)

    def _claim_next_task(self, *, execution_target: str, claimed_by: str) -> Task | None:
        if execution_target == Task.ExecutionTarget.INTERACTIVE:
            queryset = Task.objects.claimable_for_interactive()
        else:
            queryset = Task.objects.claimable_for_headless()

        task = queryset.first()
        if task is None:
            return None

        task.claim(claimed_by=claimed_by)
        return task

    @staticmethod
    def _execute_headless(task: Task) -> dict[str, str]:
        import traceback  # noqa: PLC0415 — deferred: loaded only when this command runs

        from teatree.agents.headless import run_headless  # noqa: PLC0415 — deferred: keeps command import light
        from teatree.core.headless_dispatch import loop_dispatch_refusal  # noqa: PLC0415 — lazy command import

        # Fail-closed billing guard, shared with ``execute_headless_task`` via
        # the single ``loop_dispatch_refusal`` chokepoint (souliane/teatree#1375):
        # a loop-dispatched phase task must run INTERACTIVE in the ``/loop`` slot,
        # never as a metered detached headless-SDK run here. The task is already CLAIMED by
        # ``_claim_next_task``; record a FAILED refusal attempt so it is not left
        # stuck CLAIMED under the loop slot.
        refusal = loop_dispatch_refusal(task)
        if refusal is not None:
            task.complete_with_attempt(exit_code=1, error=refusal, result={"routing_error": refusal})
            return {"exit_code": "1", "routing_error": refusal}

        # Durable failure recording, the same semantics ``execute_headless_task``
        # applies (souliane/teatree#2192): ``run_headless`` can RAISE on an SDK
        # client startup / query / response error. The task is already CLAIMED;
        # without this, the raise leaves it silently CLAIMED until lease reap, then
        # re-fires forever with NO durable failed TaskAttempt — a wedge/retry-loop
        # under the no-fallback cutover. Record a FAILED attempt carrying the error
        # via the shared ``complete_with_attempt`` recorder (which FAILs the task,
        # releasing the claim) and return a nonzero command result, mirroring the
        # refusal path above rather than re-raising and dropping the result dict.
        try:
            attempt = run_headless(
                task,
                phase=task.phase,
                overlay_skill_metadata=get_overlay_for_ticket(task.ticket).metadata.get_skill_metadata(),
            )
        except Exception:  # noqa: BLE001 — ANY SDK failure (startup/query/response) must be recorded durably, not escape.
            error = traceback.format_exc()
            logger.warning("Task %s: SDK headless run raised; recording a failed attempt", task.pk)
            task.complete_with_attempt(exit_code=1, error=error, result={"sdk_error": error})
            return {"exit_code": "1", "sdk_error": error}
        return {"exit_code": str(attempt.exit_code), "attempt_id": str(attempt.pk)}


def _task_row(task: Task) -> TaskRow:
    return TaskRow(
        task_id=task.pk,
        ticket_id=task.ticket_id,  # ty: ignore[unresolved-attribute]
        ticket_title=task.ticket.short_description,
        status=task.status,
        execution_target=task.execution_target,
        phase=task.phase,
        execution_reason=task.execution_reason,
        claimed_by=task.claimed_by,
    )


def _build_claude_command(task: Task) -> list[str]:
    """Build the ``claude`` argv for an interactive task.

    Resumes the prior session when the task carries a Claude session UUID,
    otherwise starts a fresh session with the interactive system context
    pre-loaded via ``--append-system-prompt``.
    """
    claude_bin = shutil.which("claude")
    if claude_bin is None:
        msg = "claude CLI is not installed"
        raise FileNotFoundError(msg)

    agent_id = task.session.agent_id if task.session else ""
    if agent_id and UUID_RE.match(agent_id):
        logger.info("Resuming claude session %s for task %s", agent_id, task.pk)
        return [claude_bin, "--resume", agent_id]

    overlay_skill_metadata = get_overlay_for_ticket(task.ticket).metadata.get_skill_metadata()
    skills = resolve_skill_bundle(
        phase=task.phase,
        overlay_skill_metadata=overlay_skill_metadata,
        worktree_path=dispatch_worktree_path(task.ticket),
    )
    system_context = build_interactive_context(task, skills=skills)
    return [claude_bin, "--append-system-prompt", system_context]


def _resolve_reason(*, reason: str, reason_file: pathlib.Path | None) -> str:
    if reason == "-":
        return sys.stdin.read()
    if reason:
        return reason
    if reason_file is not None:
        return reason_file.read_text()
    return ""


def _exec_inline(argv: list[str]) -> None:
    from teatree.utils.run import run_streamed  # noqa: PLC0415 — deferred: keeps command import light

    orig_cwd = os.environ.get("T3_ORIG_CWD", "")
    cwd = orig_cwd if orig_cwd and pathlib.Path(orig_cwd).is_dir() else None
    rc = run_streamed(argv, cwd=cwd, check=False)
    raise SystemExit(rc)
