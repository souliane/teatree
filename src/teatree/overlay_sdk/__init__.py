"""``teatree.overlay_sdk`` — the surface-frozen overlay-authoring CONTRACT (PR-27).

An overlay package needs a stable set of teatree symbols to subclass
:class:`OverlayBase`, build :class:`ProvisionStep` steps, declare readiness
probes, resolve a :class:`Variant`, and (the headless-FACTORY surface) register a
harness / dispatch programmatically. Historically each overlay reached into many
different ``teatree.*`` modules directly for these, so an internal rename in core
broke the overlay *asynchronously* — the overlay's own CI caught it, long after
core merged.

This module is the single documented, drift-guarded surface for that overlay-authoring
CONTRACT: the extension-point classes/protocols an overlay subclasses or implements plus
the headless-factory surface. Its ``__all__`` is the frozen contract;
:mod:`tests.teatree_overlay_sdk.test_overlay_sdk_surface` snapshots that set AND the
extension-point method signatures in **core's** CI, so a rename or a signature change fails
core *locally* — the conformance guard for extension-point drift. An overlay imports its
authoring-contract symbols from here::

    from teatree.overlay_sdk import OverlayBase, ProvisionStep, Probe, Variant

**Scope of the guarantee (#3157 AH-9).** This surface is the frozen contract for the
overlay-authoring API — NOT a literal ban on importing deeper teatree internals. A real
overlay's provisioning IMPLEMENTATION legitimately still couples to internal plumbing
(``teatree.utils.db``/``teatree.utils.django_db`` import mechanics, ``teatree.utils.ports``,
``teatree.paths``, ``teatree.core.models`` ORM types): those are deliberately NOT part of
this frozen set — they may change, and such imports are the overlay's own maintenance
surface, not a stability promise. Widening ``__all__`` to span them would freeze internals
that must stay free to evolve. What IS guaranteed frozen is the authoring contract below
plus the headless-factory seam a factory overlay dispatches through (proven spanned by
``test_overlay_sdk_surface``).

Adding to the surface is a deliberate act: extend ``__all__`` here and update the
snapshot. Removing or renaming an exported symbol is a breaking change that bumps
``__overlay_api_version__``.

The provisioning/lifecycle surface is below; the headless-FACTORY surface (#3157 E6 —
programmatic dispatch, attempt recording, harness registration, the neutral
:class:`~teatree.agents.harness_options.HarnessOptions`, budget/watchdog tuning,
Lane-B toolset registration, the context/cache plan) is re-exported from
:mod:`teatree.overlay_sdk.factory`.
"""

from teatree._overlay_api import __overlay_api_version__
from teatree.config import clone_root, discover_overlays
from teatree.core.e2e_scenario import Capture, E2eExtrasContext, Scenario
from teatree.core.gates.merge_guard import MergeGuard
from teatree.core.overlay import (
    DEFAULT_TRANSITION_EMOJIS,
    FailedE2EWatcher,
    OverlayBase,
    OverlayConfig,
    OverlayConnectors,
    OverlayE2E,
    OverlayProvisioning,
    OverlayReview,
    OverlayRuntime,
)
from teatree.core.overlay_metadata import OverlayMetadata
from teatree.core.provision.variant import Variant
from teatree.core.worktree.health import HealthCheck
from teatree.core.worktree.readiness import (
    CommandProbeSpec,
    HTTPProbeSpec,
    Probe,
    ProbeResult,
    command_probe,
    http_probe,
)
from teatree.core.worktree.worktree_env import compose_project, env_cache_path
from teatree.docker.reap import reap_compose_project
from teatree.overlay_sdk.factory import (
    AttemptUsage,
    CacheBreakpoint,
    CompactionPolicy,
    ContextPlan,
    ContextSegment,
    CostBreakdown,
    CostReport,
    Harness,
    HarnessBuildContext,
    HarnessCapabilities,
    HarnessOptions,
    HarnessSession,
    HarnessSpec,
    LaneBToolConfig,
    LoopWatchdog,
    ResultEnvelopeError,
    SegmentStability,
    TicketBudget,
    UnknownHarnessError,
    UnstableCacheHeadError,
    assert_byte_stable_head,
    build_lane_b_toolsets,
    cache_control_plan,
    find_unstable_tokens,
    headless_cost_breakdown,
    parse_result_envelope,
    record_result_envelope,
    register_harness,
    registered_harness_names,
    resolve_harness_spec,
    run_headless,
    validate_result_keys,
)
from teatree.types import (
    BaseImageConfig,
    DbImportStrategy,
    ProvisionStep,
    RunCommand,
    RunCommands,
    ServiceSpec,
    SkillMetadata,
    SymlinkSpec,
    ToolCommand,
    ValidationResult,
)
from teatree.utils.django_db import DjangoDbImportConfig
from teatree.utils.run import CommandFailedError, TimeoutExpired, run_allowed_to_fail, run_checked
from teatree.visual_qa import matches_triggers

__all__ = [
    "DEFAULT_TRANSITION_EMOJIS",
    "AttemptUsage",
    "BaseImageConfig",
    "CacheBreakpoint",
    "Capture",
    "CommandFailedError",
    "CommandProbeSpec",
    "CompactionPolicy",
    "ContextPlan",
    "ContextSegment",
    "CostBreakdown",
    "CostReport",
    "DbImportStrategy",
    "DjangoDbImportConfig",
    "E2eExtrasContext",
    "FailedE2EWatcher",
    "HTTPProbeSpec",
    "Harness",
    "HarnessBuildContext",
    "HarnessCapabilities",
    "HarnessOptions",
    "HarnessSession",
    "HarnessSpec",
    "HealthCheck",
    "LaneBToolConfig",
    "LoopWatchdog",
    "MergeGuard",
    "OverlayBase",
    "OverlayConfig",
    "OverlayConnectors",
    "OverlayE2E",
    "OverlayMetadata",
    "OverlayProvisioning",
    "OverlayReview",
    "OverlayRuntime",
    "Probe",
    "ProbeResult",
    "ProvisionStep",
    "ResultEnvelopeError",
    "RunCommand",
    "RunCommands",
    "Scenario",
    "SegmentStability",
    "ServiceSpec",
    "SkillMetadata",
    "SymlinkSpec",
    "TicketBudget",
    "TimeoutExpired",
    "ToolCommand",
    "UnknownHarnessError",
    "UnstableCacheHeadError",
    "ValidationResult",
    "Variant",
    "__overlay_api_version__",
    "assert_byte_stable_head",
    "build_lane_b_toolsets",
    "cache_control_plan",
    "clone_root",
    "command_probe",
    "compose_project",
    "discover_overlays",
    "env_cache_path",
    "find_unstable_tokens",
    "headless_cost_breakdown",
    "http_probe",
    "matches_triggers",
    "parse_result_envelope",
    "reap_compose_project",
    "record_result_envelope",
    "register_harness",
    "registered_harness_names",
    "resolve_harness_spec",
    "run_allowed_to_fail",
    "run_checked",
    "run_headless",
    "validate_result_keys",
]
