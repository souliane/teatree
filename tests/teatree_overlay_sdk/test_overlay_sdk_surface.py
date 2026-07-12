"""Snapshot guard for ``teatree.overlay_sdk`` — the frozen overlay-authoring surface (PR-27 / PR-27b).

Runs in **core's** CI so an internal rename or an extension-point signature
change fails core *locally* — the conformance guard for extension-point drift —
instead of breaking an overlay's own CI asynchronously. When the surface changes
intentionally, update ``EXPECTED_SURFACE`` / the per-facet snapshots here in the
same PR (and bump ``__overlay_api_version__`` for a removal/rename/reshape).

PR-27b regrouped the flat ``get_*`` hooks off ``OverlayBase`` into composed facet
objects (``provisioning``/``runtime``/``e2e``/``review``/``connectors``), so the
extension-point contract now spans ``OverlayBase`` PLUS the five facet classes —
each is snapshotted below.
"""

import inspect

import teatree.overlay_sdk
from teatree.core.overlay import (
    OverlayBase,
    OverlayConnectors,
    OverlayE2E,
    OverlayProvisioning,
    OverlayReview,
    OverlayRuntime,
)

overlay_sdk = teatree.overlay_sdk

EXPECTED_SURFACE: frozenset[str] = frozenset(
    {
        "DEFAULT_TRANSITION_EMOJIS",
        "AttemptUsage",
        "BaseImageConfig",
        "CacheBreakpoint",
        "CommandFailedError",
        "CommandProbeSpec",
        "CompactionPolicy",
        "ContextPlan",
        "ContextSegment",
        "CostBreakdown",
        "CostReport",
        "DbImportStrategy",
        "DjangoDbImportConfig",
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
    }
)

# The frozen signatures of the ``OverlayBase`` identity/reference surface. A
# rename, a new/removed/reordered parameter, or a changed return annotation flips
# this snapshot RED in core CI.
EXPECTED_BASE_SIGNATURES: dict[str, str] = {
    "get_checking_sources": "(self) -> list[str]",
    "get_eval_scenarios_dir": "(self) -> pathlib.Path | None",
    "get_health_signals": "(self) -> list['HealthSignal']",
    "get_issue_title": "(self, url: str) -> str",
    "get_provision_steps": "(self, worktree: 'Worktree') -> list[teatree.types.ProvisionStep]",
    "get_repos": "(self) -> list[str]",
    "get_timeouts": "(self) -> dict[str, int]",
    "get_workspace_repos": "(self) -> list[str]",
    "is_issue_done": "(self, issue_data: 'RawAPIDict') -> bool",
    "resolve_issue_token": "(self, iid: int) -> str | None",
    "resolve_mr_token": "(self, iid: int) -> str | None",
}

EXPECTED_PROVISIONING_SIGNATURES: dict[str, str] = {
    "base_images": "(self, worktree: 'Worktree') -> list[teatree.types.BaseImageConfig]",
    "cleanup_steps": "(self, worktree: 'Worktree') -> list[teatree.types.ProvisionStep]",
    "compose_file": "(self, worktree: 'Worktree') -> str",
    "db_import": (
        "(self, worktree: 'Worktree', *, force: bool = False, slow_import: bool = False, "
        "dslr_snapshot: str = '', dump_path: str = '', approve_remote_dump: bool = False) -> bool"
    ),
    "db_import_strategy": "(self, worktree: 'Worktree') -> teatree.types.DbImportStrategy | None",
    "declared_env_keys": "(self) -> set[str]",
    "declared_secret_env_keys": "(self) -> set[str]",
    "docker_services": "(self, worktree: 'Worktree') -> set[str]",
    "env_extra": "(self, worktree: 'Worktree') -> dict[str, str]",
    "envrc_lines": "(self, worktree: 'Worktree') -> list[str]",
    "health_checks": "(self, worktree: 'Worktree') -> list['HealthCheck']",
    "post_db_steps": "(self, worktree: 'Worktree') -> list[teatree.types.ProvisionStep]",
    "reap_external_resources": "(self, worktree: 'Worktree') -> list[str]",
    "reset_passwords_command": "(self, worktree: 'Worktree') -> teatree.types.ProvisionStep | None",
    "resolve_variant": "(self, name: str) -> teatree.core.provision.variant.Variant",
    "services_config": "(self, worktree: 'Worktree') -> dict[str, teatree.types.ServiceSpec]",
    "snapshot_warmer_configs": "(self) -> list['DjangoDbImportConfig']",
    "symlinks": "(self, worktree: 'Worktree') -> list[teatree.types.SymlinkSpec]",
}

EXPECTED_RUNTIME_SIGNATURES: dict[str, str] = {
    "lint_command": "(self, worktree: 'Worktree') -> list[str] | teatree.types.RunCommand",
    "pre_run_steps": "(self, worktree: 'Worktree', service: str) -> list[teatree.types.ProvisionStep]",
    "readiness_probes": "(self, worktree: 'Worktree') -> list['Probe']",
    "run_commands": "(self, worktree: 'Worktree') -> RunCommands",
    "test_command": "(self, worktree: 'Worktree') -> list[str] | teatree.types.RunCommand",
    "verify_endpoints": "(self, worktree: 'Worktree') -> dict[str, str]",
}

EXPECTED_E2E_SIGNATURES: dict[str, str] = {
    "env_extras": "(self, env_cache: dict[str, str]) -> dict[str, str]",
    "playwright_args": "(self, spec_path: str) -> list[str]",
    "preflight": ("(self, *, customer: str | None, base_url: str | None) -> list[collections.abc.Callable[[], None]]"),
    "run_provenance": "(self, spec_path: str) -> str",
    "scenarios": "(self, spec_path: str) -> tuple",
}

EXPECTED_REVIEW_SIGNATURES: dict[str, str] = {
    "can_auto_merge": "(self, *, target_ref: str, thread_ref: str) -> teatree.core.gates.merge_guard.MergeGuard",
    "classify_customer_display_impact": "(self, changed_files: list[str]) -> bool",
    "merge_candidate_repo_slugs": "(self) -> list[str]",
    "visual_qa_targets": "(self, changed_files: list[str]) -> list[str]",
}

EXPECTED_CONNECTOR_SIGNATURES: dict[str, str] = {
    "manifest": "(self) -> list['ConnectorRequirement']",
    "mcp_provider_expectations": "(self) -> dict[str, str]",
    "preflight": "(self) -> list[collections.abc.Callable[[], None]]",
}


def _normalize(signature: str) -> str:
    # ``pathlib.Path`` renders as the private ``pathlib._local.Path`` on 3.13;
    # normalise so the snapshot survives a future stdlib-internal rename without
    # weakening drift detection on any actual overlay signature.
    return signature.replace("pathlib._local.Path", "pathlib.Path")


def _signatures(cls: type) -> dict[str, str]:
    return {
        name: _normalize(str(inspect.signature(method)))
        for name, method in inspect.getmembers(cls, predicate=inspect.isfunction)
        if not name.startswith("_")
    }


# The headless-FACTORY dependency seam a factory overlay dispatches through: the pluggable
# agent runtime, the model/option binding, the capability flags, and the dispatch/cost/context
# surface. AH-9's honest claim is scoped to the overlay-AUTHORING CONTRACT (this + the extension
# points), NOT to every internal an overlay's provisioning couples to — so the guarantee we DO
# make is that this factory seam is FULLY spanned by the surface. This locks that.
FACTORY_SEAM: frozenset[str] = frozenset(
    {
        "Harness",
        "HarnessSession",
        "HarnessBuildContext",
        "HarnessCapabilities",
        "HarnessSpec",
        "HarnessOptions",
        "UnknownHarnessError",
        "register_harness",
        "registered_harness_names",
        "resolve_harness_spec",
        "run_headless",
        "LoopWatchdog",
        "TicketBudget",
        "ContextPlan",
        "ContextSegment",
        "SegmentStability",
        "CacheBreakpoint",
        "cache_control_plan",
        "assert_byte_stable_head",
        "find_unstable_tokens",
        "UnstableCacheHeadError",
        "LaneBToolConfig",
        "build_lane_b_toolsets",
        "CompactionPolicy",
        "AttemptUsage",
        "CostReport",
        "CostBreakdown",
        "headless_cost_breakdown",
        "parse_result_envelope",
        "record_result_envelope",
        "validate_result_keys",
        "ResultEnvelopeError",
    }
)


def test_surface_is_frozen():
    assert set(overlay_sdk.__all__) == EXPECTED_SURFACE


def test_every_exported_name_is_importable():
    missing = [name for name in overlay_sdk.__all__ if not hasattr(overlay_sdk, name)]
    assert missing == [], f"overlay_sdk.__all__ names that do not resolve: {missing}"


def test_factory_dependency_seam_is_fully_spanned_by_the_surface():
    """AH-9: the factory-overlay dependency seam is entirely importable from overlay_sdk.

    The honest, corrected claim is that overlay_sdk spans the overlay-AUTHORING CONTRACT + the
    headless-factory seam — not every teatree internal an overlay's provisioning couples to. This
    proves the guarantee we DO make: a factory overlay dispatches through this whole seam via the
    single surface, importing each symbol without reaching into ``teatree.agents.*`` internals.
    """
    unspanned = sorted(name for name in FACTORY_SEAM if name not in set(overlay_sdk.__all__))
    assert unspanned == [], f"factory-seam symbols missing from the overlay_sdk surface: {unspanned}"
    unresolved = sorted(name for name in FACTORY_SEAM if not hasattr(overlay_sdk, name))
    assert unresolved == [], f"factory-seam symbols not importable from overlay_sdk: {unresolved}"


def test_base_signatures_are_frozen():
    assert _signatures(OverlayBase) == EXPECTED_BASE_SIGNATURES


def test_provisioning_signatures_are_frozen():
    assert _signatures(OverlayProvisioning) == EXPECTED_PROVISIONING_SIGNATURES


def test_runtime_signatures_are_frozen():
    assert _signatures(OverlayRuntime) == EXPECTED_RUNTIME_SIGNATURES


def test_e2e_signatures_are_frozen():
    assert _signatures(OverlayE2E) == EXPECTED_E2E_SIGNATURES


def test_review_signatures_are_frozen():
    assert _signatures(OverlayReview) == EXPECTED_REVIEW_SIGNATURES


def test_connector_signatures_are_frozen():
    assert _signatures(OverlayConnectors) == EXPECTED_CONNECTOR_SIGNATURES


def test_base_is_shrunk_to_the_identity_surface():
    """PR-27b: the flat ``get_*`` provisioning/runtime/e2e/review hooks left the base."""
    base = _signatures(OverlayBase)
    assert len(base) <= 12, f"OverlayBase regrew to {len(base)} methods — regroup into a facet"
    for gone in ("env_extra", "provisioning.env_extra", "runtime.run_commands", "can_auto_merge", "e2e.env_extras"):
        assert gone not in base


def test_snapshot_detects_a_signature_change():
    """Anti-vacuity: the snapshot equality would go RED on any signature drift.

    Mutating one facet hook's signature (an added parameter here) is exactly what
    an accidental rename/reshape in core would do; the same equality check the
    guard uses must flag it, proving the guard is not vacuous.
    """
    drifted = dict(_signatures(OverlayProvisioning))
    drifted["env_extra"] = "(self, worktree: 'Worktree', extra_arg: str) -> dict[str, str]"
    assert drifted != EXPECTED_PROVISIONING_SIGNATURES
