"""Record an agent result envelope back onto a ``Task`` as a ``TaskAttempt``.

The single contract for turning a structured agent result into a terminal
``Task`` outcome, shared by two callers. ``run_headless`` is the detached
``claude -p`` subprocess path (now reserved for genuinely headless, non-loop
work). ``manage.py task record-attempt`` is the in-session ``/loop`` slot path:
after the slot's ``Agent`` sub-agent returns, the slot hands the same result
envelope here so an INTERACTIVE phase task completes (and the ticket advances)
exactly as the headless path would have.

Both go through :func:`record_result_envelope`, so the schema-key check, the
phase-evidence gate (#1284), the usage stamping, and the
``complete`` / ``fail`` decision live in ONE place and cannot drift between
the two dispatch backends.
"""

import dataclasses
import json

from django.utils import timezone

from teatree.agents.outage_classifier import outage_signature
from teatree.agents.result_schema import RESULT_JSON_SCHEMA, AgentResultBlob, check_evidence
from teatree.core.models import Task, TaskAttempt


@dataclasses.dataclass(frozen=True)
class AttemptUsage:
    """Usage stats stamped onto a recorded :class:`TaskAttempt`.

    All optional — a backend reports what it has. ``claude -p`` parses these
    from the CLI envelope; an in-session ``record-attempt`` may omit them.
    """

    agent_session_id: str = ""
    model: str = ""
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    cost_usd: float | None = None
    num_turns: int | None = None


class ResultEnvelopeError(ValueError):
    """The supplied result envelope is not a JSON object."""


def parse_result_envelope(raw: str) -> AgentResultBlob:
    """Parse a JSON result object, raising :class:`ResultEnvelopeError` otherwise.

    Accepts the exact envelope shape ``run_headless`` parses out of the agent
    text: a single JSON object whose keys are the
    :data:`~teatree.agents.result_schema.RESULT_JSON_SCHEMA` fields
    (``summary``, ``files_modified``, ``needs_user_input`` …). A non-object
    payload is rejected up front so a malformed hand-off never silently
    completes a task on an empty result.
    """
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        msg = f"result is not valid JSON: {exc}"
        raise ResultEnvelopeError(msg) from exc
    if not isinstance(parsed, dict):
        msg = "result must be a JSON object"
        raise ResultEnvelopeError(msg)
    return parsed


def validate_result_keys(result: AgentResultBlob) -> str:
    """Return an error message if *result* carries keys outside the schema.

    Only the ``additionalProperties: false`` rule is enforced (no full
    JSON-Schema dependency), mirroring the headless path's ``_validate_result``.
    """
    allowed = set(RESULT_JSON_SCHEMA.get("properties", {}).keys())  # type: ignore[union-attr]
    unexpected = set(result) - allowed
    if unexpected:
        return f"Agent result contains unexpected keys: {', '.join(sorted(unexpected))}"
    return ""


def record_result_envelope(
    task: Task,
    result: AgentResultBlob,
    *,
    phase: str = "",
    usage: AttemptUsage | None = None,
) -> TaskAttempt:
    """Record *result* as a ``TaskAttempt`` and drive the ``Task`` to terminal.

    Validation order: schema-key check → OUTAGE check (#1764) → per-phase
    evidence gate (#1284) — a failure on any records a FAILED attempt and fails
    the task (``exit_code=0`` so it reads as a clean refusal, not a crash). The
    outage check runs BEFORE the evidence gate so an outage death that happens
    to carry evidence (the "API error laundered as a completion" class) still
    lands FAILED with the diagnostic signature, never COMPLETED — the ticket FSM
    must not advance over work an outage interrupted. On success the attempt is
    COMPLETED and ``task.complete`` fires, auto-advancing the ticket FSM (a
    ``needs_user_input`` result completes the task too — ``_advance_ticket`` then
    schedules the interactive follow-up rather than firing the phase
    transition).
    """
    usage = usage or AttemptUsage()
    schema_error = validate_result_keys(result)
    if schema_error:
        return _record_failure(task, error=schema_error)

    signature = outage_signature(result)
    if signature:
        return _record_failure(task, error=f"outage_death: {signature}")

    evidence_error = check_evidence(result, phase or task.phase)
    if evidence_error:
        return _record_failure(task, error=evidence_error)

    _maybe_record_plan_artifact(task, result, phase=phase)

    attempt = TaskAttempt.objects.create(
        task=task,
        execution_target=task.execution_target,
        ended_at=timezone.now(),
        exit_code=0,
        result=result,
        agent_session_id=usage.agent_session_id,
        model=usage.model,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_tokens=usage.cache_read_tokens,
        cache_write_tokens=usage.cache_write_tokens,
        cost_usd=usage.cost_usd,
        num_turns=usage.num_turns,
    )
    task.complete(result_artifact_path="")
    return attempt


def _maybe_record_plan_artifact(task: Task, result: AgentResultBlob, *, phase: str) -> None:
    from teatree.core.models.plan_artifact import PlanArtifact  # noqa: PLC0415

    effective_phase = phase or task.phase
    plan_text = result.get("plan_text")
    if effective_phase != "planning" or not isinstance(plan_text, str) or not plan_text.strip():
        return
    PlanArtifact.record(ticket=task.ticket, plan_text=plan_text, recorded_by=task.session.agent_id or "")


def _record_failure(task: Task, *, error: str) -> TaskAttempt:
    attempt = TaskAttempt.objects.create(
        task=task,
        execution_target=task.execution_target,
        ended_at=timezone.now(),
        exit_code=0,
        error=error,
    )
    task.fail()
    return attempt
