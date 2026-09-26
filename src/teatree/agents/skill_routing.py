import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from threading import RLock

from django.db import OperationalError, ProgrammingError
from django.utils import timezone

from teatree.config.agent_spawn import AgentRouteCandidate, SkillModelPolicy
from teatree.core.modelkit.phases import normalize_phase
from teatree.core.models import AgentRouteAvailability


class AmbiguousSkillRouteError(ValueError):
    pass


class ConflictingHarnessRoutingError(ValueError):
    pass


def resolve_skill_route(
    skill_models: Mapping[str, SkillModelPolicy],
    loaded_skills: Iterable[str],
    *,
    phase_candidates: Sequence[str],
    primary_skill: str = "",
) -> tuple[str, tuple[AgentRouteCandidate, ...]] | None:
    owners = sorted(
        skill for skill in set(loaded_skills) if isinstance(skill_models.get(skill), tuple) and skill_models[skill]
    )
    if primary_skill in owners:
        owners = [primary_skill]
    elif len(owners) > 1:
        message = "multiple loaded skills own an ordered agent route: " + ", ".join(owners)
        raise AmbiguousSkillRouteError(message)
    if not owners:
        return None
    if phase_candidates:
        message = (
            f"skill {owners[0]!r} defines an ordered agent route while "
            "factory_phase_harness_candidates also owns routing"
        )
        raise ConflictingHarnessRoutingError(message)
    candidates = skill_models[owners[0]]
    if not isinstance(candidates, tuple):
        return None
    return owners[0], candidates


_FALLBACK_PATTERNS = (
    re.compile(r"\b(?:401|403|408|425|429|5\d\d)\b", re.IGNORECASE),
    re.compile(r"\b(?:auth(?:entication|orization)?|credential|quota|rate.?limit|token outage)\b", re.IGNORECASE),
    re.compile(r"\b(?:model access|access denied|not entitled|not available)\b", re.IGNORECASE),
    re.compile(r"\b(?:connection|transport|network|timed? out|timeout|temporarily unavailable)\b", re.IGNORECASE),
    re.compile(r"\b(?:spawn|process|could not start|failed to start|enoent)\b", re.IGNORECASE),
)


def runtime_fallback_reason(error: BaseException | str) -> str | None:
    text = str(error).strip()
    return text if text and any(pattern.search(text) for pattern in _FALLBACK_PATTERNS) else None


@dataclass(frozen=True, slots=True)
class RouteAvailabilityObservation:
    unavailable_reason: str | None
    observed_at: datetime
    retry_at: datetime


type _AvailabilityKey = tuple[str, str, str, str, str]


_MEMORY: dict[_AvailabilityKey, RouteAvailabilityObservation] = {}
_PERSISTENT_CHECKED: set[_AvailabilityKey] = set()
_LOCK = RLock()
# A healthy observation is deliberately process-local for at most ten minutes:
# another worker's newer outage may therefore cost this worker one failed call,
# which immediately replaces its observation through ``record_route_unavailable``.
# Re-reading durable state on every dispatch would defeat the no-DB-hot-path goal.
_AVAILABLE_TTL = timedelta(minutes=10)
_UNAVAILABLE_TTL = timedelta(minutes=2)


def _key(overlay: str, candidate: AgentRouteCandidate, phase: str) -> _AvailabilityKey:
    return overlay, candidate.harness, candidate.provider or "", candidate.model, normalize_phase(phase)


def _persistent_observation(key: _AvailabilityKey) -> RouteAvailabilityObservation | None:
    try:
        row = AgentRouteAvailability.objects.filter(
            overlay=key[0], harness=key[1], provider=key[2], model=key[3], phase=key[4]
        ).first()
    except (OperationalError, ProgrammingError):
        return None
    if row is None:
        return None
    return RouteAvailabilityObservation(row.unavailable_reason or None, row.observed_at, row.retry_at)


def _persist(key: _AvailabilityKey, observation: RouteAvailabilityObservation) -> None:
    try:
        AgentRouteAvailability.objects.update_or_create(
            overlay=key[0],
            harness=key[1],
            provider=key[2],
            model=key[3],
            phase=key[4],
            defaults={
                "unavailable_reason": observation.unavailable_reason or "",
                "observed_at": observation.observed_at,
                "retry_at": observation.retry_at,
            },
        )
    except (OperationalError, ProgrammingError):
        return


def cached_unavailable_reason(
    overlay: str,
    candidate: AgentRouteCandidate,
    probe: Callable[[], str | None],
    *,
    phase: str = "",
) -> str | None:
    key = _key(overlay, candidate, phase)
    now = timezone.now()
    with _LOCK:
        observation = _MEMORY.get(key)
        if observation is None and key not in _PERSISTENT_CHECKED:
            _PERSISTENT_CHECKED.add(key)
            observation = _persistent_observation(key)
            if observation is not None:
                _MEMORY[key] = observation
        if observation is not None and observation.retry_at > now:
            if observation.unavailable_reason:
                return observation.unavailable_reason
            # A durable/process-local healthy result cannot prove that this worker
            # still has the harness binary. Keep the DB off the hot path, but repeat
            # the cheap local capability probe before trusting cached health.
            reason = probe()
            if reason:
                unavailable = RouteAvailabilityObservation(reason, now, now + _UNAVAILABLE_TTL)
                _MEMORY[key] = unavailable
                _persist(key, unavailable)
            return reason

        reason = probe()
        ttl = _UNAVAILABLE_TTL if reason else _AVAILABLE_TTL
        observation = RouteAvailabilityObservation(reason, now, now + ttl)
        _MEMORY[key] = observation
        _persist(key, observation)
        return reason


def record_route_unavailable(
    overlay: str,
    candidate: AgentRouteCandidate,
    reason: str,
    *,
    phase: str = "",
    retry_after: timedelta = _UNAVAILABLE_TTL,
) -> None:
    key = _key(overlay, candidate, phase)
    now = timezone.now()
    observation = RouteAvailabilityObservation(reason, now, now + retry_after)
    with _LOCK:
        current = _MEMORY.get(key)
        if current is None or current.observed_at <= now:
            _MEMORY[key] = observation
            _PERSISTENT_CHECKED.add(key)
            _persist(key, observation)


def clear_route_availability_cache() -> None:
    with _LOCK:
        _MEMORY.clear()
        _PERSISTENT_CHECKED.clear()
