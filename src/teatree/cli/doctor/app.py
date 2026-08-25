"""``t3 doctor``'s Typer surface, and the compatibility re-export barrel.

The check RUN — :func:`run_doctor_checks` and its grouping helpers — lives in
:mod:`teatree.cli.doctor.run_checks` and is re-exported here; patch its probes
on THAT module, never this one. The :class:`DoctorService` /
:class:`IntrospectionHelpers` services live in :mod:`teatree.cli.doctor.service`;
the ``_check_*`` probes live in the ``checks_environment`` / ``checks_runtime`` /
``checks_mcp`` / ``checks_session`` / ``checks_loop`` modules. All are re-exported
below so existing ``from teatree.cli.doctor import _x`` /
``teatree.cli.doctor._x`` access paths stay intact.
"""

from importlib.metadata import PackageNotFoundError

import typer

from teatree.cli.doctor.checks_admission_pressure import (
    _check_box_occupancy,
    _check_drain_lane_starved,
    _check_intake_budget_deadlock,
    _check_starved_intake_candidates,
)
from teatree.cli.doctor.checks_bootstrap import (
    _check_claude_settings_drift,
    _check_gh_token_permissions,
    _check_provision_concurrency_from_host,
)
from teatree.cli.doctor.checks_cold_hooks import _check_cold_hook_settings_readable, _check_config_override_tier_healthy
from teatree.cli.doctor.checks_db_integrity import _check_database_health
from teatree.cli.doctor.checks_dead_ticket_rows import check_dead_ticket_rows
from teatree.cli.doctor.checks_docker import _check_docker_workflow_wired
from teatree.cli.doctor.checks_environment import (
    _check_configured_review_skills,
    _check_control_db_agreement,
    _check_dangling_editable_pth,
    _check_editable_sanity,
    _check_entrypoint_is_primary_clone,
    _check_legacy_overlay_alias,
    _check_single_db,
    _check_skills,
    _check_stale_path_t3,
    _check_stale_uv_venv,
    _check_t3_shim_receipt,
)
from teatree.cli.doctor.checks_gate_inertness import _check_gates_shipped_inert
from teatree.cli.doctor.checks_intent import _check_intent_freshness
from teatree.cli.doctor.checks_loop import (
    _check_aged_sweep_skips,
    _check_compose_output_root_pinned,
    _check_dream_consolidation_blocked,
    _check_dream_staleness,
    _check_dream_transcript_visibility,
    _check_intake_pass_incomplete,
    _check_loop_classification_drift,
    _check_loop_presets,
    _check_loop_schedule_liveness,
    _check_marker_jam,
    _check_shipped_seed_inertness,
    _check_t3_master_unheld_while_loops_tick,
    _check_unconsumed_merge_clears,
)
from teatree.cli.doctor.checks_mcp import (
    _check_chrome_devtools_mcp_suggestion,
    _check_connector_manifest,
    _check_mcp_connectivity,
    _check_teatree_mcp_liveness,
    _check_teatree_mcp_registration,
)
from teatree.cli.doctor.checks_mode_override import _check_mode_override_staleness
from teatree.cli.doctor.checks_provisioning import _check_declared_dependencies_provisioned
from teatree.cli.doctor.checks_recommendations import _check_recommended_skills
from teatree.cli.doctor.checks_reconciliation import _check_reconciliation_ledger
from teatree.cli.doctor.checks_resources import (
    _check_pyright_lsp_plugin,
    _check_root_disk_headroom,
    _check_scratch_sweep_probe,
    _check_tmp_tmpfs_headroom,
    _check_tmp_tmpfs_sizing,
    _check_worker_memory_cap,
    _check_worker_skills_present,
)
from teatree.cli.doctor.checks_runtime import (
    _check_singletons,
    _check_ttyd_for_dashboard,
    _check_worker_running,
    _check_worker_singleton_holder,
)
from teatree.cli.doctor.checks_session import (
    _check_account_switch,
    _check_agent_session_pins,
    _check_interactive_permission_mode,
    _check_slack_socket_mode,
)
from teatree.cli.doctor.checks_skill_pins import _check_skill_pin_freshness
from teatree.cli.doctor.checks_skill_supply import _check_dispatched_overlay_skills, _check_skill_source_drift
from teatree.cli.doctor.checks_slack_engagement import check_slack_engagement
from teatree.cli.doctor.checks_slack_roundtrip import check_slack_roundtrip
from teatree.cli.doctor.checks_stranded_prek_patches import check_stranded_prek_patches
from teatree.cli.doctor.checks_test_durations import (
    check_test_durations_coverage,
    check_test_durations_freshness,
    check_test_timeout_headroom,
)
from teatree.cli.doctor.checks_unshipped_work import check_unshipped_work
from teatree.cli.doctor.dev_sources import (
    _find_host_project_root,
    _find_teatree_pyproject_from_cwd,
    _patch_uv_source,
    _write_dev_sources_marker,
)
from teatree.cli.doctor.plugin_repair import (
    _do_ensure_plugin_registered,
    _ensure_plugin_registered,
    _read_json_safe,
    _repair_enabled_plugins,
    _repair_installed_plugins,
    _repair_marketplace_json,
    _resolve_main_clone,
)
from teatree.cli.doctor.run_checks import run_doctor_checks
from teatree.cli.doctor.service import (
    _CLAUDE_PLUGIN_ID,
    AGENT_SKILL_RUNTIMES,
    DoctorService,
    IntrospectionHelpers,
    agent_skill_dirs,
)
from teatree.cli.doctor.statusline import check_statusline, check_statusline_freshness
from teatree.cli.recommended_authorizations import authorizations

doctor_app = typer.Typer(no_args_is_help=False, help="Smoke-test hooks, imports, services.")
doctor_app.command()(authorizations)

__all__ = (
    "AGENT_SKILL_RUNTIMES",
    "_CLAUDE_PLUGIN_ID",
    "DoctorService",
    "IntrospectionHelpers",
    "PackageNotFoundError",
    "_check_account_switch",
    "_check_aged_sweep_skips",
    "_check_agent_session_pins",
    "_check_box_occupancy",
    "_check_chrome_devtools_mcp_suggestion",
    "_check_claude_settings_drift",
    "_check_cold_hook_settings_readable",
    "_check_compose_output_root_pinned",
    "_check_config_override_tier_healthy",
    "_check_configured_review_skills",
    "_check_connector_manifest",
    "_check_control_db_agreement",
    "_check_dangling_editable_pth",
    "_check_database_health",
    "_check_declared_dependencies_provisioned",
    "_check_dispatched_overlay_skills",
    "_check_docker_workflow_wired",
    "_check_drain_lane_starved",
    "_check_dream_consolidation_blocked",
    "_check_dream_staleness",
    "_check_dream_transcript_visibility",
    "_check_editable_sanity",
    "_check_entrypoint_is_primary_clone",
    "_check_gates_shipped_inert",
    "_check_gh_token_permissions",
    "_check_intake_budget_deadlock",
    "_check_intake_pass_incomplete",
    "_check_intent_freshness",
    "_check_interactive_permission_mode",
    "_check_legacy_overlay_alias",
    "_check_loop_classification_drift",
    "_check_loop_presets",
    "_check_loop_schedule_liveness",
    "_check_marker_jam",
    "_check_mcp_connectivity",
    "_check_mode_override_staleness",
    "_check_provision_concurrency_from_host",
    "_check_pyright_lsp_plugin",
    "_check_recommended_skills",
    "_check_reconciliation_ledger",
    "_check_root_disk_headroom",
    "_check_scratch_sweep_probe",
    "_check_shipped_seed_inertness",
    "_check_single_db",
    "_check_singletons",
    "_check_skill_pin_freshness",
    "_check_skill_source_drift",
    "_check_skills",
    "_check_slack_socket_mode",
    "_check_stale_path_t3",
    "_check_stale_uv_venv",
    "_check_starved_intake_candidates",
    "_check_t3_master_unheld_while_loops_tick",
    "_check_t3_shim_receipt",
    "_check_teatree_mcp_liveness",
    "_check_teatree_mcp_registration",
    "_check_tmp_tmpfs_headroom",
    "_check_tmp_tmpfs_sizing",
    "_check_ttyd_for_dashboard",
    "_check_unconsumed_merge_clears",
    "_check_worker_memory_cap",
    "_check_worker_running",
    "_check_worker_singleton_holder",
    "_check_worker_skills_present",
    "_do_ensure_plugin_registered",
    "_ensure_plugin_registered",
    "_find_host_project_root",
    "_find_teatree_pyproject_from_cwd",
    "_patch_uv_source",
    "_read_json_safe",
    "_repair_enabled_plugins",
    "_repair_installed_plugins",
    "_repair_marketplace_json",
    "_resolve_main_clone",
    "_write_dev_sources_marker",
    "agent_skill_dirs",
    "check",
    "check_dead_ticket_rows",
    "check_slack_engagement",
    "check_slack_roundtrip",
    "check_statusline",
    "check_statusline_freshness",
    "check_stranded_prek_patches",
    "check_test_durations_coverage",
    "check_test_durations_freshness",
    "check_test_timeout_headroom",
    "check_unshipped_work",
    "doctor_app",
    "run_doctor_checks",
)


@doctor_app.command()
def check(
    *,
    repair: bool = typer.Option(
        False,
        "--repair",
        help=(
            "Allow doctor to APPLY fixes that mutate state: re-point a relocated/hijacked "
            "t3 editable install (#3231), clear a stale entrypoint-seeded "
            "provision_max_concurrency pin (#3434), and re-register the t3 Claude plugin. "
            "A plain run never mutates."
        ),
    ),
    slack_roundtrip: bool = typer.Option(
        False,
        "--slack-roundtrip",
        help="Deep Slack round-trip: additionally run a LIVE auth.test per Slack backend (#3411).",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit findings as JSON for the watchdog container."),
) -> None:
    """Verify imports, required tools, and editable-install sanity."""
    if json_output:
        from teatree.cli.doctor.self_heal import check_as_json  # noqa: PLC0415 — deferred: --json path only

        ok = check_as_json(lambda: run_doctor_checks(repair=repair, slack_roundtrip=slack_roundtrip))
    else:
        ok = run_doctor_checks(repair=repair, slack_roundtrip=slack_roundtrip)
    # Standalone Click discards a command's return value, so the pass/fail bool
    # must be turned into the process exit code here — a `t3 doctor check && …`
    # in CI/hooks and the watchdog's non-JSON path both key on it (#3313).
    raise typer.Exit(code=0 if ok else 1)


@doctor_app.callback(invoke_without_command=True)
def _doctor_default(ctx: typer.Context) -> None:
    """Run ``check`` when ``t3 doctor`` is invoked with no subcommand (#2065)."""
    if ctx.invoked_subcommand is None:
        raise typer.Exit(code=0 if run_doctor_checks(repair=False) else 1)
