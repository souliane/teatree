"""Failure-attempt recording and learned availability for ordered skill routes."""

import contextlib
import traceback
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from http import HTTPStatus

from django.utils import timezone

from teatree.agents.harness_dispatch import MANAGED_CHATGPT_PROVIDER, DispatchHarness
from teatree.agents.harness_registry import HarnessFallbackKind
from teatree.agents.runner_failure_taxonomy import limit_match
from teatree.agents.runner_stream import HarnessOutcome
from teatree.agents.runner_usage import DispatchProvenance, UsageObservation, _attempt_usage
from teatree.agents.skill_routing import QUOTA_AUTH_HOLD, record_route_unavailable
from teatree.config.agent_spawn import AgentRouteCandidate
from teatree.core.models import Task, TaskAttempt
from teatree.core.models.checkout_exit import attest_checkout_on_park
from teatree.llm.anthropic_limits import SUBSCRIPTION_LIMIT_CAUSES, LimitCause, believable_refusal_reset, window_horizon


@dataclass(frozen=True, slots=True)
class RouteFallback:
    reason: str = ""
    source_attempt_id: int | None = None


NO_ROUTE_FALLBACK = RouteFallback()
_STICKY_KINDS = frozenset({HarnessFallbackKind.AUTH, HarnessFallbackKind.QUOTA, HarnessFallbackKind.QUOTA_EXHAUSTED})
_SERVER_ERROR_START = 500
_SERVER_ERROR_END = 600


@dataclass(frozen=True, slots=True)
class RouteFailureRecord:
    reason: str
    skills: list[str]
    outcome: HarnessOutcome | None = None
    lane: str = ""
    fallback: RouteFallback = NO_ROUTE_FALLBACK
    agent_session_id: str = ""
    kind: HarnessFallbackKind | None = None
    account: str = ""

    @property
    def refused_for_credit(self) -> bool:
        return self.reason.startswith(f"{LimitCause.API_CREDIT.value}:")

    @property
    def hold(self) -> timedelta | None:
        """How long the failed candidate stays out and its task parked; ``None`` is the short default."""
        if self.kind is not None:
            return QUOTA_AUTH_HOLD if self.kind in _STICKY_KINDS else None
        if self.outcome is None:
            return None
        match = limit_match(
            self.outcome.result_message,
            self.outcome.rate_limit_info,
            metered_transport=self.lane == TaskAttempt.Lane.METERED,
        )
        if match is None:
            message = self.outcome.result_message
            return (
                QUOTA_AUTH_HOLD
                if message is not None and message.api_error_status == HTTPStatus.TOO_MANY_REQUESTS
                else None
            )
        if match.cause is LimitCause.LEAK_BLOCKED:
            return None
        reset = match.stated_reset
        epoch = self.outcome.rate_limit_info.resets_at if self.outcome.rate_limit_info is not None else None
        if epoch is not None:
            with contextlib.suppress(ValueError, OverflowError, OSError):
                reset = datetime.fromtimestamp(epoch, tz=UTC)
        now = timezone.now()
        if (
            reset is not None
            and match.cause in {LimitCause.PROVIDER_ACCESS_DENIED, LimitCause.PROVIDER_BUDGET}
            and believable_refusal_reset(reset.timestamp(), now=now.timestamp()) is None
        ):
            reset = None
        if reset is not None and reset > now and window_horizon(match.cause) is not None:
            return reset - now
        return QUOTA_AUTH_HOLD


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
            context_tokens=record.outcome.context_tokens if record.outcome is not None else None,
            model_fell_back=record.outcome is not None and record.outcome.model_fell_back,
        ),
    )
    usage = replace(usage, account=record.account, agent_session_id=record.agent_session_id or usage.agent_session_id)
    return TaskAttempt.objects.create(
        task=task,
        ended_at=timezone.now(),
        exit_code=1,
        error=record.reason,
        **usage_fields(usage),
    )


def fail_routed_crash(task: Task, dispatch: DispatchHarness, skills: list[str]) -> TaskAttempt:
    """Record the exception being handled as one FAILED attempt on its route candidate, so the sweep can move on."""
    error = traceback.format_exc()
    attempt = record_route_failure_attempt(task, dispatch, RouteFailureRecord(error, skills))
    task.fail_claimed(reason=error)
    return attempt


def learn_route_failure(task: Task, dispatch: DispatchHarness, record: RouteFailureRecord, *, phase: str) -> None:
    record_route_unavailable(
        task.ticket.overlay or "",
        AgentRouteCandidate(
            dispatch.name,
            dispatch.model or "",
            dispatch.availability_provider or None,
        ),
        record.reason,
        phase=phase,
        retry_after=record.hold,
    )


def park_after_route_failure(task: Task, dispatch: DispatchHarness, record: RouteFailureRecord, *, phase: str) -> None:
    """Park *task* behind a failure that already ran side effects; a quota or auth one holds the candidate too."""
    if record.hold is not None:
        learn_route_failure(task, dispatch, record, phase=phase)
    park_until(task, timezone.now() + (record.hold or timedelta()))


def park_until(task: Task, not_before: datetime) -> None:
    attest_checkout_on_park()
    task.park(not_before=not_before)


def fallback_reason_for_outcome(outcome: HarnessOutcome, *, metered_transport: bool, lane: str = "") -> str | None:
    if outcome.driver_ended:
        return None
    matched = limit_match(outcome.result_message, outcome.rate_limit_info, metered_transport=metered_transport)
    if matched is not None:
        if matched.cause is LimitCause.LEAK_BLOCKED or (
            lane == TaskAttempt.Lane.SUBSCRIPTION and matched.cause in SUBSCRIPTION_LIMIT_CAUSES
        ):
            return None
        return matched.as_reason()
    message = outcome.result_message
    if message is None or not message.is_error:
        return None
    status = message.api_error_status
    if status in {408, 425, HTTPStatus.TOO_MANY_REQUESTS} or (
        status is not None and _SERVER_ERROR_START <= status < _SERVER_ERROR_END
    ):
        return f"api_error_status={status}"
    return None
