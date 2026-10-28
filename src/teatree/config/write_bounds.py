"""Shared numeric bounds for configuration writes and drift advisories."""

from dataclasses import dataclass
from math import inf, isfinite

from teatree.config.settings import WRITE_CONCURRENCY_PER_CORE_MAX, WRITE_CONCURRENCY_PER_CORE_MIN
from teatree.config.settings_provisioning import (
    ADMISSION_LOAD_FACTOR_MAX,
    ADMISSION_LOAD_FACTOR_MIN,
    FORGE_POOL_MAX_CONNECTIONS_MAX,
    FORGE_POOL_MAX_CONNECTIONS_MIN,
)


@dataclass(frozen=True, slots=True)
class WriteBound:
    low: float
    high: float = inf
    zero_disables: bool = False


WRITE_BOUNDS: dict[str, WriteBound] = {
    "admission_write_concurrency_per_core": WriteBound(WRITE_CONCURRENCY_PER_CORE_MIN, WRITE_CONCURRENCY_PER_CORE_MAX),
    "admission_load_brake_per_core": WriteBound(ADMISSION_LOAD_FACTOR_MIN, ADMISSION_LOAD_FACTOR_MAX),
    "admission_load_resume_per_core": WriteBound(ADMISSION_LOAD_FACTOR_MIN, ADMISSION_LOAD_FACTOR_MAX),
    "forge_pool_max_connections": WriteBound(FORGE_POOL_MAX_CONNECTIONS_MIN, FORGE_POOL_MAX_CONNECTIONS_MAX),
    "agent_max_turns": WriteBound(100, zero_disables=True),
    "pydantic_ai_request_limit": WriteBound(100, zero_disables=True),
    "watchdog_max_runtime_seconds": WriteBound(3600, zero_disables=True),
    "watchdog_max_turns": WriteBound(100, zero_disables=True),
}


def bound_refusal(key: str, value: object) -> str:
    bound = WRITE_BOUNDS.get(key)
    if bound is None:
        return ""
    if bound.zero_disables and value == 0 and type(value) is int:
        return ""
    if (
        isinstance(value, int | float)
        and not isinstance(value, bool)
        and isfinite(value)
        and bound.low <= value <= bound.high
    ):
        return ""
    if bound.high == inf:
        return f"{key} must be at least {bound.low:g} or 0 to disable, got {value!r}"
    return f"{key} must be between {bound.low:g} and {bound.high:g}, got {value!r}"
