"""Outcome classification, usage attribution, and attempt recording for the runner."""

import logging
from dataclasses import dataclass, replace

from teatree.agents.compaction_guard import COMPACTION_BLOCKED_REASON
from teatree.agents.envelope_refusal import NO_ENVELOPE_ERROR
from teatree.agents.harness import Harness
from teatree.agents.result_schema import AgentResultBlob, ProseSummaryPolicy
from teatree.agents.runner_failure_taxonomy import context_exhaustion_reason, is_context_exhaustion
from teatree.agents.runner_failure_taxonomy import error_result_reason as _error_result_reason
from teatree.agents.runner_failure_taxonomy import limit_match as _limit_match
from teatree.agents.runner_interruption import CeilingSalvage, _record_failure, _record_stuck_outcome
from teatree.agents.runner_stream import HarnessOutcome
from teatree.agents.runner_truncation import (
    alert_owner_max_tokens_truncation,
    alert_owner_max_turns_truncation,
    is_max_tokens_truncation,
    is_max_turns_truncation,
    max_turns_failure_reason,
)
from teatree.agents.runner_usage import DispatchProvenance, UsageObservation, _attempt_usage
from teatree.agents.skill_assurance import assess_skill_application
from teatree.agents.usage_window import LimitSignal, park_or_rotate_on_limit
from teatree.config import AgentHarnessProvider
from teatree.core.admission.dispatch_lane import dispatch_lane
from teatree.core.models import Task, TaskAttempt
from teatree.core.telemetry.admission import record_skill_assurance

logger = logging.getLogger(__name__)

_PROSE_SUMMARY_CHARS = 1000


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
    usage = _attempt_usage(
        outcome.result_message,
        UsageObservation(
            lane=lane,
            tool_calls=outcome.tool_calls,
            provenance=transport.provenance or DispatchProvenance(),
        ),
    )
    if outcome.stuck_reason is not None:
        return _record_stuck_outcome(task, outcome, stuck_reason=outcome.stuck_reason, usage=usage)
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


def failure_reason(outcome: HarnessOutcome) -> str | None:
    if outcome.compaction_stopped:
        return COMPACTION_BLOCKED_REASON
    if is_context_exhaustion(outcome.result_message):
        return context_exhaustion_reason(outcome.result_message)
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
        or record_success(task, outcome, phase=salvage.phase, lane=salvage.lane, provenance=salvage.provenance)
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
    provenance: DispatchProvenance | None = None,
) -> TaskAttempt:
    """Record a successful SDK run via the shared recorder."""
    from teatree.agents.attempt_recorder import record_result_envelope  # noqa: PLC0415 — avoids recorder cycle
    from teatree.agents.runner_result import parse_result  # noqa: PLC0415 — keeps result parsing off module import path

    provenance = provenance or DispatchProvenance()
    usage = _attempt_usage(
        outcome.result_message,
        UsageObservation(
            lane=lane,
            reasoning_effort=provenance.reasoning_effort,
            skills_loaded=provenance.skills_loaded,
            tool_calls=outcome.tool_calls,
            provenance=provenance,
        ),
    )
    parsed = parse_result(outcome.agent_text)
    if provenance.skill_assurance is not None:
        usage = replace(
            usage,
            skill_assurance=assess_skill_application(
                provenance.skill_assurance,
                parsed or {},
                observed_loads=outcome.observed_skill_loads,
            ),
        )
    # Application evidence is untrusted agent text. Its bounded assurance
    # classification was captured above; never persist the raw reference.
    result: AgentResultBlob = (
        {key: value for key, value in parsed.items() if key != "skill_application"} if parsed else {}
    )
    if not parsed:
        prose: AgentResultBlob = {"summary": outcome.agent_text[:_PROSE_SUMMARY_CHARS]}
        if not ProseSummaryPolicy.allowed(phase or task.phase):
            logger.warning("Task %s produced no result envelope; refusing to record success", task.pk)
            return _record_failure(task, exit_code=0, error=NO_ENVELOPE_ERROR, result=prose, usage=usage)
        result = prose
    return record_result_envelope(task, result, phase=phase, usage=usage, envelope_parsed=bool(parsed))
