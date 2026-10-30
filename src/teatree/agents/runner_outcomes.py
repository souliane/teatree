"""Outcome classification, usage attribution, and attempt recording for the runner."""

import logging
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from django.db import transaction

from teatree.agents.compaction_guard import COMPACTION_BLOCKED_REASON
from teatree.agents.credential_policy import PLANS_ONLY_MARKER
from teatree.agents.envelope_refusal import NO_ENVELOPE_ERROR
from teatree.agents.harness import Harness
from teatree.agents.result_schema import AgentResultBlob, ProseSummaryPolicy
from teatree.agents.runner_failure_taxonomy import (
    cli_too_old_fallback,
    cli_too_old_reason,
    context_exhaustion_reason,
    is_context_exhaustion,
    is_refused_resume,
    refused_resume_reason,
)
from teatree.agents.runner_failure_taxonomy import error_result_reason as _error_result_reason
from teatree.agents.runner_failure_taxonomy import limit_match as _limit_match
from teatree.agents.runner_interruption import (
    CeilingSalvage,
    _record_checkpoint,
    _record_failure,
    _record_stuck_outcome,
)
from teatree.agents.runner_stream import HarnessOutcome
from teatree.agents.runner_truncation import (
    alert_owner_max_tokens_truncation,
    alert_owner_max_turns_truncation,
    is_max_tokens_truncation,
    is_max_turns_truncation,
    max_turns_failure_reason,
)
from teatree.agents.runner_usage import DispatchProvenance, UsageObservation, _attempt_usage
from teatree.agents.skill_assurance import assess_skill_application, requested_skill_loads
from teatree.agents.usage_window import LimitSignal, park_or_rotate_on_limit
from teatree.config import AgentHarnessProvider
from teatree.core.admission.dispatch_lane import dispatch_lane
from teatree.core.models import Task, TaskAttempt
from teatree.core.telemetry.admission import record_skill_assurance

if TYPE_CHECKING:
    from teatree.agents.attempt_recorder import AttemptUsage

logger = logging.getLogger(__name__)

_PROSE_SUMMARY_CHARS = 1000
_ROUND_PARK_PREFIX = "limit_parked: round ceiling"
_ROUND_NOTE_MARKER = "[round-ceiling-continuation]"
MAX_ROUND_CONTINUATIONS = 3


@dataclass(frozen=True, slots=True)
class Transport:
    """Identity, billing shape, and dispatch provenance fixed before a turn."""

    account: str = ""
    metered: bool = False
    provenance: DispatchProvenance | None = None


UNROUTED = Transport()


def outcome_failure(
    task: Task,
    outcome: HarnessOutcome,
    *,
    phase: str = "",
    lane: str = "",
    transport: Transport = UNROUTED,
) -> TaskAttempt | None:
    """Fold a non-success drive outcome into a recorded failure or park."""
    usage = _failure_usage(outcome, lane=lane, transport=transport)
    if (stopped := _record_stop(task, outcome, usage=usage)) is not None:
        return stopped
    limit = _limit_match(outcome.result_message, outcome.rate_limit_info, metered_transport=transport.metered)
    if limit is not None:
        sdk_resets_at = outcome.rate_limit_info.resets_at if outcome.rate_limit_info is not None else None
        signal = LimitSignal(sdk_resets_at=sdk_resets_at, usage=usage, account=transport.account)
        parked = park_or_rotate_on_limit(task, limit, lane=lane, signal=signal)
        if parked is not None:
            return parked
        reason = limit.as_reason()
        logger.warning("Task %s hit a model-access limit (%s): %s", task.pk, limit.cause.value, reason)
        return _record_failure(task, error=reason, usage=usage)
    if is_max_turns_truncation(outcome.result_message):
        reason = max_turns_failure_reason(outcome.result_message)
        alert_owner_max_turns_truncation(task, phase=phase, message=outcome.result_message)
        logger.warning("Task %s stopped at the turn ceiling: %s", task.pk, reason)
        return _record_failure(task, error=reason, result=outcome.unfinished_result, usage=usage)
    error_reason = failure_reason(outcome)
    if error_reason is not None:
        if is_max_tokens_truncation(outcome.result_message):
            alert_owner_max_tokens_truncation(task, phase=phase)
        logger.warning("Task %s ended in a failed run: %s", task.pk, error_reason)
        return _record_failure(task, error=error_reason, result=outcome.unfinished_result, usage=usage)
    return None


def _record_stop(task: Task, outcome: HarnessOutcome, *, usage: "AttemptUsage") -> TaskAttempt | None:
    """A run the driver ended: a round-boundary hand-off parks, a watchdog breach or lost lease fails."""
    if outcome.round_handoff:
        return record_round_handoff(task, outcome, usage=usage)
    if outcome.stuck_reason is not None:
        return _record_stuck_outcome(task, outcome, stuck_reason=outcome.stuck_reason, usage=usage)
    return None


def record_round_handoff(task: Task, outcome: HarnessOutcome, *, usage: "AttemptUsage") -> TaskAttempt:
    """Requeue a bounded fresh round after a driver-enforced round ceiling."""
    logger.warning("Task %s stopped at a round boundary: %s", task.pk, outcome.round_handoff)
    prior = sum(
        1 + repeats
        for repeats in TaskAttempt.objects.filter(task=task, error__startswith=_ROUND_PARK_PREFIX).values_list(
            "park_repeats", flat=True
        )
    )
    if prior >= MAX_ROUND_CONTINUATIONS:
        return _record_failure(task, error=f"round_ceiling_exhausted: {outcome.round_handoff}", usage=usage)
    summary = outcome.agent_text.strip()[-_PROSE_SUMMARY_CHARS:] or "Stopped at a round boundary."
    note = (
        f"{_ROUND_NOTE_MARKER} Fresh round {prior + 1}: check the branch and its pipeline first; "
        f"the prior round stopped at {outcome.round_handoff}. Its last progress: {summary}"
    )
    with transaction.atomic():
        attempt = _record_checkpoint(
            task, checkpoint=f"round ceiling: {outcome.round_handoff}", usage=usage, summary=note
        )
        if Task.objects.filter(pk=task.pk, status=Task.Status.PENDING).exists():
            base = task.execution_reason.partition(_ROUND_NOTE_MARKER)[0].rstrip()
            if PLANS_ONLY_MARKER in task.execution_reason and PLANS_ONLY_MARKER not in base:
                base = f"{base}\n{PLANS_ONLY_MARKER}".strip()
            reason = f"{base}\n\n{note}" if base else note
            Task.objects.filter(pk=task.pk).update(execution_reason=reason)
            task.execution_reason = reason
    return attempt


def cli_too_old_failure(task: Task, outcome: HarnessOutcome, *, lane: str, transport: Transport) -> TaskAttempt | None:
    """Fail a run the CLI served on a fallback because its build cannot serve the requested model (#4874)."""
    fallback = cli_too_old_fallback(outcome.model_fallbacks)
    if fallback is None:
        return None
    reason = cli_too_old_reason(fallback)
    logger.error("Task %s ran on a fallback model the CLI forced: %s", task.pk, reason)
    return _record_failure(task, error=reason, usage=_failure_usage(outcome, lane=lane, transport=transport))


def _failure_usage(outcome: HarnessOutcome, *, lane: str, transport: Transport) -> "AttemptUsage":
    observation = UsageObservation(
        lane=lane,
        tool_calls=outcome.tool_calls,
        provenance=transport.provenance or DispatchProvenance(),
        context_tokens=outcome.context_tokens,
        model_fell_back=outcome.model_fell_back,
    )
    usage = replace(_attempt_usage(outcome.result_message, observation), account=transport.account)
    if outcome.session_id and not usage.agent_session_id:
        usage = replace(usage, agent_session_id=outcome.session_id)
    receipt = usage.skill_assurance
    if receipt is None:
        return usage
    return replace(
        usage,
        skill_assurance={**receipt, "observed_loads": requested_skill_loads(receipt, outcome.observed_skill_loads)},
    )


def failure_reason(outcome: HarnessOutcome) -> str | None:
    if outcome.compaction_stopped:
        return COMPACTION_BLOCKED_REASON
    if is_context_exhaustion(outcome.result_message):
        return context_exhaustion_reason(outcome.result_message)
    if outcome.resumed and is_refused_resume(outcome.result_message):
        return refused_resume_reason(outcome.result_message)
    return _error_result_reason(outcome.result_message)


def record_outcome(
    task: Task,
    outcome: HarnessOutcome,
    harness: Harness,
    salvage: CeilingSalvage,
    *,
    transport: Transport = UNROUTED,
) -> TaskAttempt:
    """Record a salvaged, failed, parked, or successful drive."""
    kept = salvage.kept(task, outcome) if harness.capabilities.spawns_cli_child else None
    routed_transport = replace(transport, provenance=salvage.provenance)
    attempt = (
        kept
        or outcome_failure(
            task,
            outcome,
            phase=salvage.phase,
            lane=salvage.lane,
            transport=routed_transport,
        )
        or cli_too_old_failure(task, outcome, lane=salvage.lane, transport=routed_transport)
        or record_success(task, outcome, phase=salvage.phase, lane=salvage.lane, transport=routed_transport)
    )
    record_skill_assurance_attempt(task, attempt)
    return attempt


def record_skill_assurance_attempt(task: Task, attempt: TaskAttempt) -> None:
    """Mirror a persisted, bounded receipt into OTel without trusting agent prose."""
    receipt = attempt.result.get("skill_assurance") if isinstance(attempt.result, dict) else None
    if isinstance(receipt, dict):
        record_skill_assurance(
            task_id=task.pk,
            ticket_id=task.ticket.pk,
            attempt_id=attempt.pk,
            assurance=receipt,
        )


def resolve_dispatch_lane(harness: Harness, provider: AgentHarnessProvider | None) -> str:
    """Return the accounting lane the selected harness/provider authenticates through.

    The provider mapping lives in :mod:`teatree.core.admission.dispatch_lane` so the governor
    judges a dispatch against the same lane the attempt is stamped with (#4816).
    """
    if harness.capabilities.managed_lane:
        return TaskAttempt.Lane.MANAGED
    return dispatch_lane(provider=provider, metered_harness=harness.capabilities.metered_lane)


def record_success(
    task: Task,
    outcome: HarnessOutcome,
    *,
    phase: str = "",
    lane: str = "",
    transport: Transport = UNROUTED,
) -> TaskAttempt:
    """Record a successful SDK run via the shared recorder."""
    from teatree.agents.attempt_recorder import record_result_envelope  # noqa: PLC0415 — avoids recorder cycle
    from teatree.agents.runner_result import (  # noqa: PLC0415 — keeps result parsing off module import path
        ResultEnvelopeError,
        declares_owner_stop,
        parse_result,
    )

    provenance = transport.provenance or DispatchProvenance()
    usage = _attempt_usage(
        outcome.result_message,
        UsageObservation(
            lane=lane,
            reasoning_effort=provenance.reasoning_effort,
            skills_loaded=provenance.skills_loaded,
            tool_calls=outcome.tool_calls,
            provenance=provenance,
            context_tokens=outcome.context_tokens,
            model_fell_back=outcome.model_fell_back,
        ),
    )
    usage = replace(usage, account=transport.account)
    parse_error: ResultEnvelopeError | None = None
    try:
        parsed = parse_result(outcome.agent_text)
    except ResultEnvelopeError as exc:
        parsed = {}
        parse_error = exc
    if provenance.skill_assurance is not None:
        prior_loads = (
            task.attempts.skill_loads_in_session(outcome.result_message.session_id)
            if outcome.resumed and outcome.result_message is not None and outcome.result_message.session_id
            else []
        )
        usage = replace(
            usage,
            skill_assurance=assess_skill_application(
                provenance.skill_assurance,
                parsed or {},
                observed_loads=(*prior_loads, *outcome.observed_skill_loads),
            ),
        )
    # Application evidence is untrusted agent text. Its bounded assurance
    # classification was captured above; never persist the raw reference.
    result: AgentResultBlob = (
        {key: value for key, value in parsed.items() if key != "skill_application"} if parsed else {}
    )
    if not parsed:
        prose: AgentResultBlob = {"summary": outcome.agent_text[:_PROSE_SUMMARY_CHARS]}
        if (
            parse_error
            and ProseSummaryPolicy.accepted(phase or task.phase)
            and not declares_owner_stop(outcome.agent_text)
        ):
            logger.warning("Task %s produced a malformed result envelope: %s", task.pk, parse_error)
            parse_error = None
        if not parse_error and not ProseSummaryPolicy.allowed(phase or task.phase):
            logger.warning("Task %s produced no result envelope; refusing to record success", task.pk)
            return _record_failure(task, exit_code=0, error=NO_ENVELOPE_ERROR, result=prose, usage=usage)
        result = prose
    return record_result_envelope(task, result, phase=phase, usage=usage, envelope_parsed=parse_error or bool(parsed))
