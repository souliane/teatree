"""Record an agent result envelope back onto a ``Task`` as a ``TaskAttempt``.

The single contract for turning a structured agent result into a terminal
``Task`` outcome, shared by two callers. ``run_agent`` is the detached
``claude -p`` subprocess path (now reserved for genuinely headless, non-loop
work). ``manage.py task record-attempt`` is the in-session ``/loop`` slot path:
after the slot's ``Agent`` sub-agent returns, the slot hands the same result
envelope here so an INTERACTIVE phase task completes (and the ticket advances)
exactly as the agent path would have.

Both go through :func:`record_result_envelope`, so the schema-key check, the
phase-evidence gate (#1284), the usage stamping, and the
``complete`` / ``fail`` decision live in ONE place and cannot drift between
the two dispatch backends.
"""

import dataclasses
import json
from collections.abc import Mapping
from typing import TYPE_CHECKING, TypedDict, cast

from django.utils import timezone

from teatree.agents.action_verification import action_verification_error
from teatree.agents.coding_result_salvage import salvage_coding_result
from teatree.agents.envelope_refusal import NO_ENVELOPE_ERROR
from teatree.agents.fix_record_recorder import record_returned_fix_record
from teatree.agents.landing_verification import landing_verification_error
from teatree.agents.outage_classifier import outage_signature
from teatree.agents.plan_artifact_recorder import record_returned_plan
from teatree.agents.reactive_envelope_recorders import record_reactive_envelopes
from teatree.agents.result_schema import RESULT_JSON_SCHEMA, AgentResultBlob, JSONSchema, check_evidence
from teatree.agents.review_envelope_recorder import record_returned_review_envelope
from teatree.core.answering.work_intent import missing_work_item_error
from teatree.core.gates.critic_gate import record_returned_critic_verdict
from teatree.core.gates.directive_interpret_gate import record_returned_directive_interpretation
from teatree.core.modelkit.phases import normalize_phase
from teatree.core.models import Task, TaskAttempt

if TYPE_CHECKING:
    from teatree.agents.pydantic_ai_turn import ToolCallEntry
    from teatree.llm.usage_tee import RequestRecord


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
    # souliane/teatree#657: the Layer-2 lane (``TaskAttempt.Lane``) this
    # attempt's credential authenticated through, or ``""`` when unattributed.
    lane: str = ""
    # #3157 E5: whether ``cost_usd`` is a price-table ESTIMATE (True) rather than a real
    # reported (CLI/SDK/metered-router) figure. Default True so a recorder path that does
    # not compute a reported cost is flagged conservatively as an estimate.
    cost_is_estimated: bool = True
    # #4816: turns ran but the provider reported no spend — recorded as a positive
    # marker so the metered ledger can tell it from a park that billed nothing.
    usage_unknown: bool = False
    # #3673 Tier 3 dispatch provenance: the per-tier reasoning effort the spawn
    # resolved and the resolved skill-bundle names. Both empty on a recorder path
    # that has no dispatch context (e.g. an in-session record-attempt).
    reasoning_effort: str = ""
    skills_loaded: list[str] = dataclasses.field(default_factory=list)
    skill_assurance: Mapping[str, object] | None = None
    selected_harness: str = ""
    selected_provider: str = ""
    selected_model: str = ""
    route_candidate_index: int | None = None
    route_source_skill: str = ""
    fallback_reason: str = ""
    fallback_from_attempt_id: int | None = None
    # Tool calls the run emitted. ``None`` means UNMEASURED — the in-session
    # ``record-attempt`` path hands over a sub-agent's envelope and never saw its
    # tool stream — and is deliberately distinct from a measured ``0``, which is
    # the positive evidence that the run could not act
    # (:mod:`teatree.agents.action_verification`).
    tool_calls: int | None = None
    # The per-request usage records the run's transport measured (``teatree.llm.usage_tee``);
    # empty on a lane that measures none.
    usage_per_request: "list[RequestRecord]" = dataclasses.field(default_factory=list)
    # The run's tool-call trajectory (``pydantic_ai_session._StreamedToolCapture``); empty when unmeasured.
    trajectory: "list[ToolCallEntry]" = dataclasses.field(default_factory=list)


class ResultEnvelopeError(ValueError):
    """The supplied result envelope is not a JSON object."""


def parse_result_envelope(raw: str) -> AgentResultBlob:
    """Parse a JSON result object, raising :class:`ResultEnvelopeError` otherwise.

    Accepts the exact envelope shape ``run_agent`` parses out of the agent
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

    The single validation seam for every agent result — the headless driver and
    ``record-attempt`` both land here. Only the ``additionalProperties: false``
    rule is enforced (no full JSON-Schema dependency).
    """
    allowed = set(cast("JSONSchema", RESULT_JSON_SCHEMA.get("properties", {})).keys())
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
    envelope_parsed: bool = True,
) -> TaskAttempt:
    """Record *result* as a ``TaskAttempt`` and drive the ``Task`` to terminal.

    Validation order: schema-key check → OUTAGE check (#1764) → ACTION check
    (an acting phase must have touched a tool) → per-phase evidence gate (#1284) →
    LANDING check (coding/debugging must have committed) → the PLAN record (a planning
    envelope whose plan is refused fails the attempt, never the recorder) —
    a failure on any records a FAILED attempt and fails the task (``exit_code=0``
    so it reads as a clean refusal, not a crash). The action check runs BEFORE the
    evidence gate so a toolless run never reaches the coding salvage below: on a
    long-lived branch the salvage would otherwise synthesize ``files_modified``
    from the whole branch diff and complete a run that never acted
    (:mod:`teatree.agents.action_verification`). The landing check re-reads the
    ticket worktree's git state so a coder that reported ``files_modified`` while
    nothing was committed (the yield-without-landing stall) lands FAILED with a
    ``landing_unverified`` diagnostic — which the bounded auto-requeue sweep then
    retries-if-transient / escalates, instead of the ticket FSM silently
    advancing over unlanded work. The
    outage check runs BEFORE the evidence gate so an outage death that happens
    to carry evidence (the "API error laundered as a completion" class) still
    lands FAILED with the diagnostic signature, never COMPLETED — the ticket FSM
    must not advance over work an outage interrupted.

    *envelope_parsed* is how the RUNNER tells this recorder whether the blob it
    hands over was actually parsed out of the agent's output. It is ``False``
    only for the manufactured ``{"summary": <prose>}`` of a run that emitted no
    JSON at all: the evidence gate then refuses "you omitted `tests_run`", which
    reads as an envelope with a missing key and is not what happened. On an
    evidence phase that refusal is therefore re-diagnosed as
    :data:`~teatree.agents.envelope_refusal.NO_ENVELOPE_ERROR`. The salvage still
    runs first, so a coder that landed real work is still rescued; the in-session
    ``record-attempt`` path parsed its own envelope and keeps the per-field
    diagnosis by default. On success the attempt is
    COMPLETED and ``task.complete`` fires, auto-advancing the ticket FSM (a
    ``needs_user_input`` result completes the task too — ``_advance_ticket`` then
    schedules the interactive follow-up rather than firing the phase
    transition).
    """
    usage = usage or AttemptUsage()
    checked = _check_before_recording(task, result, phase=phase, usage=usage, envelope_parsed=envelope_parsed)
    result = checked.result
    if checked.error:
        return _record_failure(task, error=checked.error, result=result, usage=usage)

    server_side_error = _record_returned_envelopes(task, result, phase=phase)
    if server_side_error:
        return _record_failure(task, error=server_side_error, result=result, usage=usage)

    plan_refusal = record_returned_plan(task, result, phase=phase)
    if plan_refusal:
        return _record_failure(task, error=plan_refusal, result=result, usage=usage)

    record_reactive_envelopes(task, result, phase=phase)

    attempt = TaskAttempt.objects.create(
        task=task,
        ended_at=timezone.now(),
        exit_code=0,
        result=with_transport_records(result, usage),
        **usage_fields(usage),
    )
    task.complete(result_artifact_path="")
    return attempt


class SpendColumns(TypedDict, total=False):
    """The ``TaskAttempt`` columns describing what a run cost. Absent = not recorded."""

    agent_session_id: str
    model: str
    input_tokens: int | None
    output_tokens: int | None
    cache_read_tokens: int | None
    cache_write_tokens: int | None
    cost_usd: float | None
    num_turns: int | None
    lane: str
    cost_is_estimated: bool
    usage_unknown: bool
    reasoning_effort: str
    skills_loaded: list[str]
    selected_harness: str
    selected_provider: str
    selected_model: str
    route_candidate_index: int | None
    route_source_skill: str
    fallback_reason: str
    fallback_from_attempt_id: int | None


def usage_fields(usage: AttemptUsage | None) -> SpendColumns:
    """The ``TaskAttempt`` spend columns for *usage* — or NONE of them when there is none.

    One mapping the success and failure recorders share, because they diverged: only the
    success path wrote spend, so every post-turn failure (a lost lease, an evidence-gate
    refusal, a harness crash) discarded tokens already billed — a measured 916 rows, and
    zero of 8,217 failed attempts in the table's history carry a token count (#4164).

    ``None`` writes nothing, leaving the columns NULL. That is the whole point of the
    distinction: a pre-turn park never spent, and a zero there would be a WORSE lie than a
    NULL because a zero reads as a measurement.
    """
    if usage is None:
        return SpendColumns()
    return SpendColumns(
        agent_session_id=usage.agent_session_id,
        model=usage.model,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_tokens=usage.cache_read_tokens,
        cache_write_tokens=usage.cache_write_tokens,
        cost_usd=usage.cost_usd,
        num_turns=usage.num_turns,
        lane=usage.lane,
        cost_is_estimated=usage.cost_is_estimated,
        usage_unknown=usage.usage_unknown,
        reasoning_effort=usage.reasoning_effort,
        skills_loaded=list(usage.skills_loaded),
        selected_harness=usage.selected_harness,
        selected_provider=usage.selected_provider,
        selected_model=usage.selected_model,
        route_candidate_index=usage.route_candidate_index,
        route_source_skill=usage.route_source_skill,
        fallback_reason=usage.fallback_reason,
        fallback_from_attempt_id=usage.fallback_from_attempt_id,
    )


def with_transport_records(result: AgentResultBlob | None, usage: AttemptUsage | None) -> AgentResultBlob:
    """*result* plus the per-request usage and tool trajectory the run measured — beside the envelope, never in it."""
    blob: AgentResultBlob = dict(result or {})
    # Every recorder path (success, salvage, lease loss, pre-run refusal) lands
    # through here. Never persist agent-supplied application references; the
    # bounded assurance receipt stores only a declaration marker.
    blob.pop("skill_application", None)
    if usage is not None and usage.usage_per_request:
        blob["usage_per_request"] = list(usage.usage_per_request)
    if usage is not None and usage.trajectory:
        blob["tool_calls"] = list(usage.trajectory)
    if usage is not None and usage.skill_assurance is not None:
        blob["skill_assurance"] = dict(usage.skill_assurance)
    return blob


@dataclasses.dataclass(frozen=True, slots=True)
class _PreRecordCheck:
    """The first refusal of the side-effect-free validation chain, and the result to record.

    ``result`` is the possibly-SALVAGED envelope: the #3263 coding salvage replaces a
    missing ``files_modified`` with the landed commit's paths, and the caller must
    record that envelope rather than the one it passed in.
    """

    error: str
    result: AgentResultBlob


def _check_before_recording(
    task: Task,
    result: AgentResultBlob,
    *,
    phase: str,
    usage: AttemptUsage,
    envelope_parsed: bool,
) -> _PreRecordCheck:
    """Run the ordered refusal chain that precedes any recording side effect.

    Order is load-bearing and documented on :func:`record_result_envelope`. Split
    out so that function stays a short record-or-refuse decision over ONE verdict
    rather than a ladder of early returns.
    """
    schema_error = validate_result_keys(result)
    if schema_error:
        return _PreRecordCheck(schema_error, result)

    signature = outage_signature(result)
    if signature:
        return _PreRecordCheck(f"outage_death: {signature}", result)

    action_error = action_verification_error(phase or task.phase, tool_calls=usage.tool_calls)
    if action_error:
        return _PreRecordCheck(action_error, result)

    evidence_error = check_evidence(result, phase or task.phase) or _answering_work_item_error(task, result, phase)
    if evidence_error:
        salvaged = salvage_coding_result(task, result, phase=phase)
        if salvaged is None:
            return _PreRecordCheck(evidence_error if envelope_parsed else NO_ENVELOPE_ERROR, result)
        result = salvaged

    return _PreRecordCheck(landing_verification_error(task, phase=phase), result)


def _answering_work_item_error(task: Task, result: AgentResultBlob, phase: str) -> str:
    """The answering phase's task-conditional evidence check — empty on every other phase."""
    if normalize_phase(phase or task.phase) != "answering":
        return ""
    return missing_work_item_error(task, result)


def _record_returned_envelopes(task: Task, result: AgentResultBlob, *, phase: str) -> str:
    """Record every shell-denied hand-back that carries a maker≠checker write, short-circuit.

    A headless phase denied the shell RETURNS its typed verdict/sketch instead of
    writing it; the orchestrator (a different actor) records it here. Each recorder is
    a no-op unless its own dispatch/verdict is present on the task, and returns an
    error string when the returned artifact is malformed or maker-graded — the first
    such error stops the chain so the caller fails the task and the block surfaces.
    """
    review_error = record_returned_review_envelope(task, result, phase=phase)
    if review_error:
        return review_error
    critic_error = record_returned_critic_verdict(task, result)
    if critic_error:
        return critic_error
    fix_error = record_returned_fix_record(task, result)
    return fix_error or record_returned_directive_interpretation(task, result)


def _record_failure(
    task: Task,
    *,
    error: str,
    result: AgentResultBlob | None = None,
    usage: AttemptUsage | None = None,
) -> TaskAttempt:
    attempt = TaskAttempt.objects.create(
        task=task,
        ended_at=timezone.now(),
        exit_code=0,
        error=error,
        result=with_transport_records(result, usage),
        **usage_fields(usage),
    )
    task.fail_claimed(reason=error)
    return attempt
