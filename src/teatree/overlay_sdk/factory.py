"""Factory-builder surface of the overlay SDK (#3157 E6).

A headless "code factory" overlay drives high-volume phase dispatches against large repos.
Before this surface it reached into private ``teatree.agents.*`` internals for the moving
parts — programmatic dispatch, attempt recording, harness registration, budget/watchdog
tuning, Lane-B toolset registration, the context/cache plan. Those are promoted here as the
DOCUMENTED, stable factory surface, re-exported from :mod:`teatree.overlay_sdk`; an
import-linter contract forbids an overlay reaching the private ``teatree.agents._*`` modules
directly (see ``pyproject.toml`` § "Overlays must not import private agents internals").

An overlay ships its own transport as::

    from teatree.overlay_sdk import HarnessCapabilities, HarnessSpec, register_harness

and drives a dispatch → attempt → cost cycle entirely through these symbols.

Re-exports resolve through :pep:`562` ``__getattr__`` rather than eager imports: this is a
FACADE, and importing it eagerly pulled ``teatree.agents.harness`` — and through it
``pydantic_ai.models.openai`` and the whole ``openai`` SDK — into every process that merely
touched the overlay SDK, including ``t3 mcp serve``, whose client times out the handshake.
Attribute access is unchanged for callers; only the moment of import moves.
"""

import importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from teatree.agents.attempt_recorder import (
        AttemptUsage,
        ResultEnvelopeError,
        parse_result_envelope,
        record_result_envelope,
        validate_result_keys,
    )
    from teatree.agents.context_plan import (
        CacheBreakpoint,
        ContextPlan,
        ContextSegment,
        SegmentStability,
        UnstableCacheHeadError,
        assert_byte_stable_head,
        cache_control_plan,
        find_unstable_tokens,
    )
    from teatree.agents.harness import Harness, HarnessSession
    from teatree.agents.harness_options import HarnessOptions
    from teatree.agents.harness_registry import (
        HarnessBuildContext,
        HarnessCapabilities,
        HarnessSpec,
        UnknownHarnessError,
        register_harness,
        registered_harness_names,
        resolve_harness_spec,
    )
    from teatree.agents.lane_b.compaction import CompactionPolicy
    from teatree.agents.lane_b.config import LaneBToolConfig
    from teatree.agents.lane_b.toolsets import build_lane_b_toolsets
    from teatree.agents.runner import LoopWatchdog, run_agent
    from teatree.agents.runner_budget import TicketBudget
    from teatree.core.cost import CostBreakdown, CostReport

_EXPORT_SOURCES: dict[str, str] = {
    "AttemptUsage": "teatree.agents.attempt_recorder",
    "ResultEnvelopeError": "teatree.agents.attempt_recorder",
    "parse_result_envelope": "teatree.agents.attempt_recorder",
    "record_result_envelope": "teatree.agents.attempt_recorder",
    "validate_result_keys": "teatree.agents.attempt_recorder",
    "CacheBreakpoint": "teatree.agents.context_plan",
    "ContextPlan": "teatree.agents.context_plan",
    "ContextSegment": "teatree.agents.context_plan",
    "SegmentStability": "teatree.agents.context_plan",
    "UnstableCacheHeadError": "teatree.agents.context_plan",
    "assert_byte_stable_head": "teatree.agents.context_plan",
    "cache_control_plan": "teatree.agents.context_plan",
    "find_unstable_tokens": "teatree.agents.context_plan",
    "Harness": "teatree.agents.harness",
    "HarnessSession": "teatree.agents.harness",
    "HarnessOptions": "teatree.agents.harness_options",
    "HarnessBuildContext": "teatree.agents.harness_registry",
    "HarnessCapabilities": "teatree.agents.harness_registry",
    "HarnessSpec": "teatree.agents.harness_registry",
    "UnknownHarnessError": "teatree.agents.harness_registry",
    "register_harness": "teatree.agents.harness_registry",
    "registered_harness_names": "teatree.agents.harness_registry",
    "resolve_harness_spec": "teatree.agents.harness_registry",
    "LoopWatchdog": "teatree.agents.runner",
    "run_agent": "teatree.agents.runner",
    "TicketBudget": "teatree.agents.runner_budget",
    "CompactionPolicy": "teatree.agents.lane_b.compaction",
    "LaneBToolConfig": "teatree.agents.lane_b.config",
    "build_lane_b_toolsets": "teatree.agents.lane_b.toolsets",
    "CostBreakdown": "teatree.core.cost",
    "CostReport": "teatree.core.cost",
}


def __getattr__(name: str) -> object:
    """Resolve a re-exported symbol on first access (:pep:`562`)."""
    source = _EXPORT_SOURCES.get(name)
    if source is None:
        msg = f"module {__name__!r} has no attribute {name!r}"
        raise AttributeError(msg)
    return getattr(importlib.import_module(source), name)


def __dir__() -> list[str]:
    return sorted(__all__)


def agent_cost_breakdown() -> "CostBreakdown":
    """The SDK-equivalent cost breakdown across every agent attempt (#3157 E5/E6).

    The cost half of a factory overlay's dispatch → attempt → cost cycle: aggregates the
    recorded :class:`~teatree.core.models.task_attempt.TaskAttempt` usage into totals split
    per model tier and per Layer-2 lane, with the estimated-vs-reported split and the
    per-lane/phase cache-hit ratios. A thin, overlay-facing wrapper so a factory reads cost
    through the SDK rather than the model manager directly.
    """
    from teatree.core.models.task_attempt import TaskAttempt  # noqa: PLC0415 — defer the Django model import

    return TaskAttempt.objects.cost_breakdown()


__all__ = [
    "AttemptUsage",
    "CacheBreakpoint",
    "CompactionPolicy",
    "ContextPlan",
    "ContextSegment",
    "CostBreakdown",
    "CostReport",
    "Harness",
    "HarnessBuildContext",
    "HarnessCapabilities",
    "HarnessOptions",
    "HarnessSession",
    "HarnessSpec",
    "LaneBToolConfig",
    "LoopWatchdog",
    "ResultEnvelopeError",
    "SegmentStability",
    "TicketBudget",
    "UnknownHarnessError",
    "UnstableCacheHeadError",
    "agent_cost_breakdown",
    "assert_byte_stable_head",
    "build_lane_b_toolsets",
    "cache_control_plan",
    "find_unstable_tokens",
    "parse_result_envelope",
    "record_result_envelope",
    "register_harness",
    "registered_harness_names",
    "resolve_harness_spec",
    "run_agent",
    "validate_result_keys",
]
