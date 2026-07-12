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
"""

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
from teatree.agents.headless import LoopWatchdog, run_headless
from teatree.agents.headless_budget import TicketBudget
from teatree.agents.lane_b.compaction import CompactionPolicy
from teatree.agents.lane_b.config import LaneBToolConfig
from teatree.agents.lane_b.toolsets import build_lane_b_toolsets
from teatree.core.cost import CostBreakdown, CostReport


def headless_cost_breakdown() -> CostBreakdown:
    """The SDK-equivalent cost breakdown across every headless attempt (#3157 E5/E6).

    The cost half of a factory overlay's dispatch → attempt → cost cycle: aggregates the
    recorded :class:`~teatree.core.models.task_attempt.TaskAttempt` usage into totals split
    per model tier and per Layer-2 lane, with the estimated-vs-reported split and the
    per-lane/phase cache-hit ratios. A thin, overlay-facing wrapper so a factory reads cost
    through the SDK rather than the model manager directly.
    """
    from teatree.core.models.task_attempt import TaskAttempt  # noqa: PLC0415 — defer the Django model import

    return TaskAttempt.objects.headless().cost_breakdown()


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
    "assert_byte_stable_head",
    "build_lane_b_toolsets",
    "cache_control_plan",
    "find_unstable_tokens",
    "headless_cost_breakdown",
    "parse_result_envelope",
    "record_result_envelope",
    "register_harness",
    "registered_harness_names",
    "resolve_harness_spec",
    "run_headless",
    "validate_result_keys",
]
