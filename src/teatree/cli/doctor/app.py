"""``t3 doctor`` Typer group + the ``check`` orchestrator.

The :class:`DoctorService` / :class:`IntrospectionHelpers` services live in
:mod:`teatree.cli.doctor.service`; the ``_check_*`` probes live in the
``checks_environment`` / ``checks_runtime`` / ``checks_mcp`` / ``checks_session``
/ ``checks_loop`` modules. All are re-exported below so existing
``from teatree.cli.doctor import _x`` / ``teatree.cli.doctor._x`` access paths
stay intact.
"""

import shutil
from importlib.metadata import PackageNotFoundError

import typer

from teatree.cli.doctor.checks_availability import _check_availability_override_staleness
from teatree.cli.doctor.checks_bootstrap import (
    _check_claude_settings_drift,
    _check_gh_token_permissions,
    _check_provision_concurrency_from_host,
    run_bootstrap_checks,
)
from teatree.cli.doctor.checks_docker import _check_docker_workflow_wired
from teatree.cli.doctor.checks_environment import (
    _check_configured_review_skills,
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
from teatree.cli.doctor.checks_loop import (
    _check_dream_staleness,
    _check_dream_transcript_visibility,
    _check_loop_presets,
    _check_marker_jam,
)
from teatree.cli.doctor.checks_mcp import (
    _check_chrome_devtools_mcp_suggestion,
    _check_connector_manifest,
    _check_mcp_connectivity,
    _check_teatree_mcp_registration,
)
from teatree.cli.doctor.checks_resources import (
    _check_pyright_lsp_plugin,
    _check_tmp_tmpfs_headroom,
    _check_worker_memory_cap,
    _check_worker_skills_present,
)
from teatree.cli.doctor.checks_runtime import _check_singletons, _check_ttyd_for_dashboard, _check_worker_running
from teatree.cli.doctor.checks_session import (
    _check_account_switch,
    _check_agent_session_pins,
    _check_interactive_permission_mode,
    _check_slack_socket_mode,
)
from teatree.cli.doctor.checks_slack_roundtrip import check_slack_roundtrip
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
from teatree.cli.doctor.service import (
    _CLAUDE_PLUGIN_ID,
    AGENT_SKILL_RUNTIMES,
    DoctorService,
    IntrospectionHelpers,
    agent_skill_dirs,
)
from teatree.cli.doctor.statusline import check_statusline
from teatree.cli.recommended_authorizations import authorizations, report_missing_authorizations
from teatree.cli.slack.dm_doctor import check_and_render_dm_ready
from teatree.utils.django_bootstrap import ensure_django

doctor_app = typer.Typer(no_args_is_help=False, help="Smoke-test hooks, imports, services.")
doctor_app.command()(authorizations)
_REQUIRED_TOOLS = ("direnv", "git", "jq")

__all__ = (
    "AGENT_SKILL_RUNTIMES",
    "_CLAUDE_PLUGIN_ID",
    "_REQUIRED_TOOLS",
    "DoctorService",
    "IntrospectionHelpers",
    "PackageNotFoundError",
    "_check_account_switch",
    "_check_agent_session_pins",
    "_check_availability_override_staleness",
    "_check_chrome_devtools_mcp_suggestion",
    "_check_claude_settings_drift",
    "_check_configured_review_skills",
    "_check_connector_manifest",
    "_check_dangling_editable_pth",
    "_check_docker_workflow_wired",
    "_check_dream_staleness",
    "_check_dream_transcript_visibility",
    "_check_editable_sanity",
    "_check_entrypoint_is_primary_clone",
    "_check_gh_token_permissions",
    "_check_interactive_permission_mode",
    "_check_legacy_overlay_alias",
    "_check_loop_presets",
    "_check_marker_jam",
    "_check_mcp_connectivity",
    "_check_provision_concurrency_from_host",
    "_check_pyright_lsp_plugin",
    "_check_single_db",
    "_check_singletons",
    "_check_skills",
    "_check_slack_socket_mode",
    "_check_stale_path_t3",
    "_check_stale_uv_venv",
    "_check_t3_shim_receipt",
    "_check_teatree_mcp_registration",
    "_check_tmp_tmpfs_headroom",
    "_check_ttyd_for_dashboard",
    "_check_worker_memory_cap",
    "_check_worker_running",
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
    "check_slack_roundtrip",
    "check_statusline",
    "doctor_app",
)


@doctor_app.command()
def check(
    *,
    repair: bool = typer.Option(
        False,
        "--repair",
        help=(
            "Allow doctor to APPLY fixes that mutate state: re-point a relocated/hijacked "
            "t3 editable install (#3231) AND clear a stale entrypoint-seeded "
            "provision_max_concurrency pin (#3434). A plain run never mutates."
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


def _optional_tooling_advisories() -> None:
    """Surfacing-only advisories for optional tooling (never gate the exit code).

    #3263 WARNs when this box serves the admin dashboard but its loopback
    "Debug session" terminal's ttyd is missing. #3271 INFO-suggests the OPTIONAL
    chrome-devtools MCP e2e/debug aid when absent (teatree's runtime requires zero
    MCP). #3232 WARNs when the operator opted into the containerized ``t3`` workflow
    but a piece the wrapper depends on (compose stack, the executable ``deploy/t3``
    entry, the docker CLI, a non-drifted alias path) is missing — silent inside a
    container and on a host that never opted in. The tmpfs-headroom check WARNs when
    a RAM-backed ``/tmp`` is filling toward ENOSPC (the fill that wedges the box);
    runtime temp is routed to disk and the watchdog trims stale scratch on a cadence.
    The pyright-lsp advisory WARNs when the plugin that gives agents live pyright type
    diagnostics is not enabled, or its ``pyright-langserver`` is missing from PATH.
    (The critical worker gates — skills-present and memory-adequate — are HARD FAILs
    in :func:`run_doctor_checks`, not advisories here.)
    """
    _check_ttyd_for_dashboard()
    _check_chrome_devtools_mcp_suggestion()
    _check_pyright_lsp_plugin()
    _check_docker_workflow_wired()
    _check_tmp_tmpfs_headroom()


def _run_worker_gates() -> bool:
    """The worker-role gates: flock liveness + the CRITICAL skills/memory HARD gates.

    ``_check_worker_running`` warns when the loop is enabled but no worker holds
    the flock. ``_check_worker_skills_present`` / ``_check_worker_memory_cap`` are the
    role-aware HARD FAILs (worker only, else OK) that refuse to let a skill-less or
    OOM-prone worker read as healthy — mirroring the entrypoint's worker startup
    precondition. Each runs independently (no short-circuit) so every finding is emitted;
    returns their AND for the caller's ``ok`` aggregation.
    """
    running = _check_worker_running()
    skills = _check_worker_skills_present()
    memory = _check_worker_memory_cap()
    return running and skills and memory


def _check_claude_session_posture() -> bool:
    """The Claude-session checks: account-switch recovery, then permission posture.

    Grouped because both read the operator's live Claude session rather than
    teatree's own state, and both must run AFTER ``ensure_django`` — the
    account-switch probe builds messaging backends through the overlay factory to
    live-probe connector reachability once the cache is invalidated. Only the
    account-switch half can hard-FAIL; the permission-mode half is advisory.
    """
    ok = _check_account_switch()
    return _check_interactive_permission_mode() and ok


def run_doctor_checks(*, repair: bool = False, slack_roundtrip: bool = False) -> bool:
    """Run every doctor check; return ``False`` if any hard-FAILs.

    The pure-boolean core the ``check`` command turns into the process exit code.
    Direct callers — ``_doctor_default`` and the ``--json`` surface — reuse it so
    the pass/fail verdict is computed in exactly one place. ``slack_roundtrip``
    turns on the deep live-``auth.test`` mode of the Slack round-trip gate (#3411).
    """
    try:
        import django  # noqa: PLC0415, F401 — deferred: Django import at call time; re-export

        import teatree.core  # noqa: PLC0415, F401 — deferred: keeps CLI startup light; re-export
    except ImportError as exc:
        typer.echo(f"FAIL  Import check: {exc}")
        return False

    ok = True
    for tool in _REQUIRED_TOOLS:
        if not shutil.which(tool):
            typer.echo(f"FAIL  Required tool not found: {tool}")
            ok = False

    # Must precede _check_editable_sanity: under contribute=true that check can
    # auto-make-editable against the cwd worktree, creating the exact stale
    # worktree-anchored install this guard exists to catch (#1507).
    ok = _check_entrypoint_is_primary_clone() and ok
    # Detect/repair a dangling editable .pth (or uv-receipt source) pointing at a
    # reaped worktree before it wedges t3 machine-wide with ModuleNotFoundError.
    ok = _check_dangling_editable_pth() and ok
    # Detect a relocated/same-name-hijacked editable install: the active t3 shim's
    # uv receipt editable source no longer matches the expected checkout ($T3_REPO).
    # Unlike the dangling check, this catches a target that EXISTS but is wrong.
    # `--repair` re-points it via `uv tool install --editable <checkout> --force`.
    ok = _check_t3_shim_receipt(repair=repair) and ok
    # ``check`` is a plain Typer command in the Django-free CLI group, so Django is
    # not configured on entry. The editable-vs-contribute check reads the DB-home
    # ``contribute`` setting via ``get_effective_settings()``, whose DB tier fails
    # safe to empty (→ the ``False`` default) when Django is unconfigured — so
    # without this every editable install saw a spurious "editable but
    # contribute=false" WARN despite a stored ``contribute=true`` row (#3213).
    # Configure Django before any check that reads the ConfigSetting store.
    ensure_django()
    ok = _check_editable_sanity() and ok
    ok = _check_skills() and ok
    # #3352: the configured review skills (review_skill / architectural_review_skill)
    # must resolve to an installed SKILL.md. Runs after ensure_django() above — it
    # reads the effective ConfigSetting-store values, whose DB tier is live only
    # once Django is configured.
    ok = _check_configured_review_skills() and ok
    ok = _check_single_db() and ok
    ok = _check_stale_uv_venv() and ok
    ok = _check_stale_path_t3() and ok
    ok = _check_agent_session_pins() and ok
    # Verify the Claude Code statusLine block (PR-17): present, absolute path,
    # executable target — with exact remediation. A missing block is a WARN
    # (`t3 setup` installs it); a relative/non-executable one is a hard FAIL.
    ok = check_statusline() and ok

    # Django was configured above (before the editable-sanity check) so the
    # self-DB schema guard reports the REAL pending-migration state rather than
    # silently WARNing on ``ImproperlyConfigured`` and masking a stale runtime
    # self-DB that locks out the merge path (#126).
    from teatree.core.gates.schema_guard import doctor_check_self_db_migrations  # noqa: PLC0415 — lazy CLI import

    ok = doctor_check_self_db_migrations() and ok

    # Worker-role gates: flock liveness (advisory) + the CRITICAL skills-present
    # and memory-adequate HARD FAILs (role-aware no-ops off the worker). See
    # :func:`_run_worker_gates` — each is evaluated independently so every finding shows.
    ok = _run_worker_gates() and ok

    # Optional-tooling advisories (ttyd / chrome-devtools MCP / containerized-t3
    # wiring) — all surfacing-only, never gating the exit code.
    _optional_tooling_advisories()

    # H24 self-heal (owner directive #10): the hard-FAIL silent-freeze detectors —
    # dead compose containers, a free worker flock over overdue loop work, a
    # stranded headless task, a stale loop timer, an unrunnable interactive task
    # under headless runtime, a failed task on a live ticket, a drifted runtime
    # clone. These flip the exit code the watchdog container (deploy/watchdog.sh) keys on.
    from teatree.cli.doctor.self_heal import run_self_heal_checks  # noqa: PLC0415 — deferred: keeps CLI startup light

    ok = run_self_heal_checks() and ok

    # #3159: warn on a dangling loop-preset reference (deleted preset / loop /
    # schedule). Surfacing-only (never gates the exit code), like the sibling
    # ORM-reading advisories below: the by-name references fail OPEN at read time
    # (resolve to base config), so a dangling target is a WARN to fix, not a hard
    # doctor failure. Reads the ORM, so it runs post-ensure_django.
    _check_loop_presets()

    # #3275: warn when orphaned issue-markers strand the issue_implementer intake
    # budget (their tickets are terminal/gone but the markers never left
    # `dispatched`). Surfacing-only (never gates the exit code), like the sibling
    # ORM-reading advisories: the loop self-heals each tick and the operator can
    # force it with `t3 loop reclaim-markers`. Reads the ORM, so it runs
    # post-ensure_django.
    _check_marker_jam()

    # Fresh-box bootstrap-hardening gates (umbrella #3404): the GitHub token lacks a
    # write permission the loop needs (#3405, hard FAIL); a stale small-box
    # provision_max_concurrency pin carried onto a bigger host (#3409/#3434,
    # entrypoint-seeded pins auto-cleared ONLY under --repair; an operator pin is
    # WARNed, never deleted); host ~/.claude/settings.json drifted from the committed
    # template (#3410, WARN). Post-ensure_django — the concurrency autofix reads the ORM.
    ok = run_bootstrap_checks(repair=repair) and ok

    # Pre-investigation stale-clone hard-fail gate (#948). Surfaces at
    # session start so a bug-investigation sub-agent cannot start root-
    # causing against a clone many commits behind ``origin/<default>``.
    # Distinct from #940 (post-implementation branch-currency on PR
    # branches); this is the *entry-point* gate before any investigation
    # reads source files. An offline/missing remote is a valid state —
    # ``doctor_check_clone_currency`` skips affected repos rather than
    # FAILing (same posture as schema_guard's DB-offline WARN).
    from teatree.cli.update import _collect_repos  # noqa: PLC0415 — deferred: keeps CLI startup light
    from teatree.core.gates.clone_guard import doctor_check_clone_currency  # noqa: PLC0415 — deferred: lazy CLI import

    ok = doctor_check_clone_currency(_collect_repos()) and ok

    # Idle-time dream consolidation staleness alarm (#1933). Runs after
    # ``ensure_django`` because it reads the ``DreamRunMarker`` row. A WARN
    # (not a hard FAIL): a stale dream cron means memories pile up unpromoted,
    # which the operator should fix, but it must not red the whole doctor run.
    _check_dream_staleness()
    _check_dream_transcript_visibility()

    # #3274: WARN on a no-expiry away / autonomous_away availability override that
    # has sat past the staleness threshold — it silently suppresses the
    # colleague-facing loops (and pauses the self-pump under holiday-away) the
    # whole time. Surfacing-only (never gates the exit code); reads the Loop table
    # for the deferred loop names, so it runs post-ensure_django.
    _check_availability_override_staleness()

    # Slack Socket Mode readiness (#106 / BLUEPRINT § B5). Extends the Slack scope
    # auto-management to the app-level (xapp-) token + socket-mode manifest: it
    # reports and auto-fixes (via apps.manifest.update) where the Slack API allows,
    # and surfaces a single actionable message for the one thing Slack cannot
    # self-provision — minting the app-level token. Surfacing-only (never gates the
    # exit code): Slack is optional and must never become mandatory.
    _check_slack_socket_mode()

    # Slack DM-readiness. Fail-loud diagnosis of an overlay declaring
    # messaging_backend=slack that still cannot message/read its owner: a no-op
    # backend (tokens missing), an empty slack_user_id, or an unprovisioned DM
    # channel. Runs after ``ensure_django`` because it builds messaging backends
    # via the overlay factory. Surfacing-only (never gates the exit code): Slack
    # is optional and must never become mandatory.
    check_and_render_dm_ready()

    # Slack round-trip comms (#3411): the "reacts-but-never-answers" detector. When
    # a Slack backend is configured, actively verify the FULL loop — outbound egress
    # resolves, the owner id resolves (the empty-string headless bug), the listener
    # is live, the inbox answer loop is enabled+unmasked with a worker draining it,
    # and no real message sits reacted-👀 but unanswered. Unlike the surfacing-only
    # DM-readiness check above, THIS gates the exit code: a silent round-trip break
    # must be a doctor FAILURE, not a surprise. `--slack-roundtrip` adds a live
    # auth.test. A silent no-op with no Slack-backed overlay (Slack stays optional).
    ok = check_slack_roundtrip(deep=slack_roundtrip) and ok

    ok = _check_claude_session_posture() and ok

    # Enabled-MCP connectivity + declared-provider check (#2282). Runs after the
    # account-switch gate (which invalidates the backend cache on a `/login`), so
    # the live `claude mcp list` probe reflects the post-recovery state. An
    # enabled-but-disconnected MCP is a hard FAIL; `claude` absent degrades to a WARN.
    ok = _check_mcp_connectivity() and ok

    # Per-overlay claude.ai connector manifest (PR-19). Runs after the general MCP
    # connectivity gate — it reuses the same live `claude mcp list` probe. A REQUIRED
    # declared connector that is down is a hard FAIL with mode-correct guidance +
    # RECONNECT lines; an optional one WARNs; `claude` absent degrades to a WARN.
    ok = _check_connector_manifest() and ok

    # Teatree's own structured-search MCP server registration (#2863). WARN-only
    # (never gates the exit code) — the resolved main clone can legitimately lag
    # a merged change until the next `t3 update`. Runs after the general MCP
    # connectivity gate — it reuses the same live `claude mcp list` probe.
    _check_teatree_mcp_registration()

    _check_singletons()
    _check_legacy_overlay_alias()
    report_missing_authorizations(typer.echo)
    _ensure_plugin_registered()

    if ok:
        typer.echo("All checks passed")
    return ok


@doctor_app.callback(invoke_without_command=True)
def _doctor_default(ctx: typer.Context) -> None:
    """Run ``check`` when ``t3 doctor`` is invoked with no subcommand (#2065)."""
    if ctx.invoked_subcommand is None:
        raise typer.Exit(code=0 if run_doctor_checks(repair=False) else 1)
