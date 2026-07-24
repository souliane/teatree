"""Names the SDK facade resolves lazily via PEP 562.

``teatree.overlay_sdk.factory`` re-exports ``teatree.agents.*`` leaves that
import ORM models at module level. Overlay entry-point modules import the
facade during CLI assembly — before ``django.setup()`` — so eagerly importing
the factory there raises ``AppRegistryNotReady``. The facade's ``__getattr__``
resolves these on first access instead, always from a command body or test with
Django up.
"""

FACTORY_EXPORTS = frozenset(
    {
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
    }
)

__all__ = ["FACTORY_EXPORTS"]
