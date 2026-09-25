"""Failure-attempt recording and learned availability for ordered skill routes."""

from dataclasses import dataclass, replace

from django.utils import timezone

from teatree.agents.harness_dispatch import MANAGED_CHATGPT_PROVIDER, DispatchHarness
from teatree.agents.runner_failure_taxonomy import limit_match
from teatree.agents.runner_stream import HarnessOutcome
from teatree.agents.runner_usage import DispatchProvenance, UsageObservation, _attempt_usage
from teatree.agents.skill_routing import record_route_unavailable, runtime_fallback_reason
from teatree.config.agent_spawn import AgentRouteCandidate
from teatree.core.models import Task, TaskAttempt


@dataclass(frozen=True, slots=True)
class RouteFallback:
    reason: str = ""
    source_attempt_id: int | None = None


NO_ROUTE_FALLBACK = RouteFallback()


@dataclass(frozen=True, slots=True)
class RouteFailureRecord:
    reason: str
    skills: list[str]
    outcome: HarnessOutcome | None = None
    lane: str = ""
    fallback: RouteFallback = NO_ROUTE_FALLBACK
    agent_session_id: str = ""


def selected_fallback_reason(fallback: RouteFallback, dispatch: DispatchHarness) -> str:
    if fallback.reason:
        return fallback.reason
    if dispatch.route_candidate_index is not None and dispatch.rejected:
        return str(dispatch.rejected[-1])
    return ""


def dispatch_provider_name(dispatch: DispatchHarness) -> str:
    if dispatch.provider is not None:
        return dispatch.provider.value
    if dispatch.harness.capabilities.managed_lane:
        return MANAGED_CHATGPT_PROVIDER
    return dispatch.availability_provider


def record_route_failure_attempt(
    task: Task,
    dispatch: DispatchHarness,
    record: RouteFailureRecord,
) -> TaskAttempt:
    from teatree.agents.attempt_recorder import usage_fields  # noqa: PLC0415 — avoids recorder cycle

    provenance = DispatchProvenance(
        reasoning_effort=dispatch.effort or "",
        skills_loaded=tuple(record.skills),
        selected_harness=dispatch.name,
        selected_provider=dispatch_provider_name(dispatch),
        selected_model=dispatch.model or "",
        route_candidate_index=dispatch.route_candidate_index,
        route_source_skill=dispatch.route_source_skill,
        fallback_reason=record.fallback.reason,
        fallback_from_attempt_id=record.fallback.source_attempt_id,
    )
    usage = _attempt_usage(
        record.outcome.result_message if record.outcome is not None else None,
        UsageObservation(
            lane=record.lane,
            tool_calls=record.outcome.tool_calls if record.outcome is not None else 0,
            provenance=provenance,
        ),
    )
    if record.agent_session_id:
        usage = replace(usage, agent_session_id=record.agent_session_id)
    return TaskAttempt.objects.create(
        task=task,
        ended_at=timezone.now(),
        exit_code=1,
        error=record.reason,
        **usage_fields(usage),
    )


def learn_route_failure(task: Task, dispatch: DispatchHarness, reason: str, *, phase: str) -> None:
    record_route_unavailable(
        task.ticket.overlay or "",
        AgentRouteCandidate(
            dispatch.name,
            dispatch.model or "",
            dispatch.availability_provider or None,
        ),
        reason,
        phase=phase,
    )


def fallback_reason_for_outcome(outcome: HarnessOutcome, *, metered_transport: bool) -> str | None:
    matched = limit_match(outcome.result_message, outcome.rate_limit_info, metered_transport=metered_transport)
    if matched is not None:
        return matched.as_reason()
    from teatree.agents.runner_outcomes import failure_reason  # noqa: PLC0415 — avoids route/outcome import cycle

    reason = failure_reason(outcome)
    return runtime_fallback_reason(reason or "")
