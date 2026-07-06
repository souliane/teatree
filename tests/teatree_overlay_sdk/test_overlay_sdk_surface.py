"""Snapshot guard for ``teatree.overlay_sdk`` — the frozen overlay-authoring surface (PR-27).

Runs in **core's** CI so an internal rename or an extension-point signature
change fails core *locally* — the conformance guard for extension-point drift —
instead of breaking an overlay's own CI asynchronously. When the surface changes
intentionally, update ``EXPECTED_SURFACE`` / ``EXPECTED_SIGNATURES`` here in the
same PR (and bump ``__overlay_api_version__`` for a removal/rename).
"""

import inspect

import teatree.overlay_sdk
from teatree.core.overlay import OverlayBase

overlay_sdk = teatree.overlay_sdk

EXPECTED_SURFACE: frozenset[str] = frozenset(
    {
        "DEFAULT_TRANSITION_EMOJIS",
        "BaseImageConfig",
        "CommandFailedError",
        "CommandProbeSpec",
        "DbImportStrategy",
        "DjangoDbImportConfig",
        "FailedE2EWatcher",
        "HTTPProbeSpec",
        "HealthCheck",
        "MergeGuard",
        "OverlayBase",
        "OverlayConfig",
        "OverlayMetadata",
        "Probe",
        "ProbeResult",
        "ProvisionStep",
        "RunCommand",
        "RunCommands",
        "ServiceSpec",
        "SkillMetadata",
        "SymlinkSpec",
        "TimeoutExpired",
        "ToolCommand",
        "ValidationResult",
        "Variant",
        "__overlay_api_version__",
        "clone_root",
        "command_probe",
        "compose_project",
        "discover_overlays",
        "env_cache_path",
        "http_probe",
        "matches_triggers",
        "reap_compose_project",
        "run_allowed_to_fail",
        "run_checked",
    }
)

# The frozen signatures of every PUBLIC ``OverlayBase`` extension hook. A rename,
# a new/removed/reordered parameter, or a changed return annotation on any hook
# flips this snapshot RED in core CI.
EXPECTED_SIGNATURES: dict[str, str] = {
    "can_auto_merge": "(self, *, target_ref: str, thread_ref: str) -> teatree.core.gates.merge_guard.MergeGuard",
    "classify_customer_display_impact": "(self, changed_files: list[str]) -> bool",
    "db_import": (
        "(self, worktree: 'Worktree', *, force: bool = False, slow_import: bool = False, "
        "dslr_snapshot: str = '', dump_path: str = '', approve_remote_dump: bool = False) -> bool"
    ),
    "declared_env_keys": "(self) -> set[str]",
    "declared_secret_env_keys": "(self) -> set[str]",
    "get_base_images": "(self, worktree: 'Worktree') -> list[teatree.types.BaseImageConfig]",
    "get_checking_sources": "(self) -> list[str]",
    "get_cleanup_steps": "(self, worktree: 'Worktree') -> list[teatree.types.ProvisionStep]",
    "get_compose_file": "(self, worktree: 'Worktree') -> str",
    "get_connector_manifest": "(self) -> list['ConnectorRequirement']",
    "get_connector_preflight": "(self) -> list[collections.abc.Callable[[], None]]",
    "get_db_import_strategy": "(self, worktree: 'Worktree') -> teatree.types.DbImportStrategy | None",
    "get_docker_services": "(self, worktree: 'Worktree') -> set[str]",
    "get_e2e_env_extras": "(self, env_cache: dict[str, str]) -> dict[str, str]",
    "get_e2e_playwright_args": "(self, spec_path: str) -> list[str]",
    "get_e2e_preflight": (
        "(self, *, customer: str | None, base_url: str | None) -> list[collections.abc.Callable[[], None]]"
    ),
    "get_e2e_run_provenance": "(self, spec_path: str) -> str",
    "get_e2e_scenarios": "(self, spec_path: str) -> tuple",
    "get_env_extra": "(self, worktree: 'Worktree') -> dict[str, str]",
    "get_envrc_lines": "(self, worktree: 'Worktree') -> list[str]",
    "get_eval_scenarios_dir": "(self) -> pathlib.Path | None",
    "get_health_checks": "(self, worktree: 'Worktree') -> list['HealthCheck']",
    "get_health_signals": "(self) -> list['HealthSignal']",
    "get_issue_title": "(self, url: str) -> str",
    "get_lint_command": "(self, worktree: 'Worktree') -> list[str] | teatree.types.RunCommand",
    "get_mcp_provider_expectations": "(self) -> dict[str, str]",
    "get_merge_candidate_repo_slugs": "(self) -> list[str]",
    "get_post_db_steps": "(self, worktree: 'Worktree') -> list[teatree.types.ProvisionStep]",
    "get_pre_run_steps": "(self, worktree: 'Worktree', service: str) -> list[teatree.types.ProvisionStep]",
    "get_provision_steps": "(self, worktree: 'Worktree') -> list[teatree.types.ProvisionStep]",
    "get_readiness_probes": "(self, worktree: 'Worktree') -> list['Probe']",
    "get_repos": "(self) -> list[str]",
    "get_reset_passwords_command": "(self, worktree: 'Worktree') -> teatree.types.ProvisionStep | None",
    "get_run_commands": "(self, worktree: 'Worktree') -> RunCommands",
    "get_services_config": "(self, worktree: 'Worktree') -> dict[str, teatree.types.ServiceSpec]",
    "get_snapshot_warmer_configs": "(self) -> list['DjangoDbImportConfig']",
    "get_symlinks": "(self, worktree: 'Worktree') -> list[teatree.types.SymlinkSpec]",
    "get_test_command": "(self, worktree: 'Worktree') -> list[str] | teatree.types.RunCommand",
    "get_timeouts": "(self) -> dict[str, int]",
    "get_verify_endpoints": "(self, worktree: 'Worktree') -> dict[str, str]",
    "get_visual_qa_targets": "(self, changed_files: list[str]) -> list[str]",
    "get_workspace_repos": "(self) -> list[str]",
    "is_issue_done": "(self, issue_data: 'RawAPIDict') -> bool",
    "reap_worktree_external_resources": "(self, worktree: 'Worktree') -> list[str]",
    "resolve_issue_token": "(self, iid: int) -> str | None",
    "resolve_mr_token": "(self, iid: int) -> str | None",
    "resolve_variant": "(self, name: str) -> teatree.core.provision.variant.Variant",
}


def _normalize(signature: str) -> str:
    # ``pathlib.Path`` renders as the private ``pathlib._local.Path`` on 3.13;
    # normalise so the snapshot survives a future stdlib-internal rename without
    # weakening drift detection on any actual overlay signature.
    return signature.replace("pathlib._local.Path", "pathlib.Path")


def _overlay_signatures() -> dict[str, str]:
    return {
        name: _normalize(str(inspect.signature(method)))
        for name, method in inspect.getmembers(OverlayBase, predicate=inspect.isfunction)
        if not name.startswith("_")
    }


def test_surface_is_frozen():
    assert set(overlay_sdk.__all__) == EXPECTED_SURFACE


def test_every_exported_name_is_importable():
    missing = [name for name in overlay_sdk.__all__ if not hasattr(overlay_sdk, name)]
    assert missing == [], f"overlay_sdk.__all__ names that do not resolve: {missing}"


def test_extension_point_signatures_are_frozen():
    assert _overlay_signatures() == EXPECTED_SIGNATURES


def test_snapshot_detects_a_signature_change():
    """Anti-vacuity: the snapshot equality would go RED on any signature drift.

    Mutating one hook's signature (an added parameter here) is exactly what an
    accidental rename/reshape in core would do; the same equality check the guard
    uses must flag it, proving the guard is not vacuous.
    """
    drifted = dict(_overlay_signatures())
    drifted["get_repos"] = "(self, extra_arg: str) -> list[str]"
    assert drifted != EXPECTED_SIGNATURES
