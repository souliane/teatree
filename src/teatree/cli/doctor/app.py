"""``t3 doctor`` Typer group + the ``check`` orchestrator.

The :class:`DoctorService` / :class:`IntrospectionHelpers` services live in
:mod:`teatree.cli.doctor.service`; the ``_check_*`` probes live in the
``checks_environment`` / ``checks_runtime`` / ``checks_mcp`` / ``checks_session``
/ ``checks_loop`` modules. All are re-exported below so existing
``from teatree.cli.doctor import _x`` / ``teatree.cli.doctor._x`` access paths
stay intact.
"""

from importlib.metadata import PackageNotFoundError

import typer

from teatree.cli.doctor.checks_availability import _check_availability_override_staleness
from teatree.cli.doctor.checks_bootstrap import (
    _check_claude_settings_drift,
    _check_gh_token_permissions,
    _check_provision_concurrency_from_host,
    run_bootstrap_checks,
)
from teatree.cli.doctor.checks_cold_hooks import _check_cold_hook_settings_readable
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
from teatree.cli.doctor.checks_intent import _check_intent_freshness
from teatree.cli.doctor.checks_loop import (
    _check_compose_output_root_pinned,
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
from teatree.cli.doctor.checks_provisioning import _check_declared_dependencies_provisioned
from teatree.cli.doctor.checks_reconciliation import _check_reconciliation_ledger
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
from teatree.cli.doctor.checks_slack_engagement import check_slack_engagement
from teatree.cli.doctor.checks_slack_roundtrip import check_slack_roundtrip
from teatree.cli.doctor.checks_worktree_health import check_worktree_health
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
from teatree.cli.doctor.statusline import check_statusline, check_statusline_freshness
from teatree.cli.recommended_authorizations import authorizations, report_missing_authorizations
from teatree.cli.slack.dm_doctor import check_and_render_dm_ready
from teatree.utils.django_bootstrap import ensure_django

doctor_app = typer.Typer(no_args_is_help=False, help="Smoke-test hooks, imports, services.")
doctor_app.command()(authorizations)

__all__ = (
    "AGENT_SKILL_RUNTIMES",
    "_CLAUDE_PLUGIN_ID",
    "DoctorService",
    "IntrospectionHelpers",
    "PackageNotFoundError",
    "_check_account_switch",
    "_check_agent_session_pins",
    "_check_availability_override_staleness",
    "_check_chrome_devtools_mcp_suggestion",
    "_check_claude_settings_drift",
    "_check_cold_hook_settings_readable",
    "_check_compose_output_root_pinned",
    "_check_configured_review_skills",
    "_check_connector_manifest",
    "_check_control_db_agreement",
    "_check_dangling_editable_pth",
    "_check_declared_dependencies_provisioned",
    "_check_docker_workflow_wired",
    "_check_dream_staleness",
    "_check_dream_transcript_visibility",
    "_check_editable_sanity",
    "_check_entrypoint_is_primary_clone",
    "_check_gh_token_permissions",
    "_check_intent_freshness",
    "_check_interactive_permission_mode",
    "_check_legacy_overlay_alias",
    "_check_loop_presets",
    "_check_marker_jam",
    "_check_mcp_connectivity",
    "_check_provision_concurrency_from_host",
    "_check_pyright_lsp_plugin",
    "_check_reconciliation_ledger",
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
    "check_slack_engagement",
    "check_slack_roundtrip",
    "check_statusline",
    "check_statusline_freshness",
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
    (The critical worker gates — skills-present, memory-adequate, and the enabled
    pyright-lsp plugin's langserver being provisioned — are HARD FAILs in
    :func:`run_doctor_checks`, not advisories here.)
    """
    _check_ttyd_for_dashboard()
    _check_chrome_devtools_mcp_suggestion()
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


def _run_loop_intent_gates() -> bool:
    """The ORM-reading loop/intent checks, grouped to keep ``run_doctor_checks`` lean.

    ``_check_loop_presets`` (#3159, dangling preset/loop/schedule refs) and
    ``_check_marker_jam`` (#3275, orphaned issue-markers stranding the intake budget)
    are surfacing-only WARNs — their return values are deliberately discarded so
    neither can become a gate by accident. ``_check_intent_freshness`` is the "no
    owner-intent silently rots" gate: it HARD-FAILs when a consumable intent queue is
    non-empty while its consumer is not live — masked/disabled/held, or refused by the
    consumer's own guard chain (the directive-loop silent-freeze incident — directives
    stuck at CAPTURED behind an idle loop, zero signal), so its verdict IS returned for
    the caller's ``ok`` aggregation.
    """
    _check_loop_presets()
    _check_marker_jam()
    return _check_intent_freshness()


def _check_claude_session_posture() -> bool:
    """The Claude-session checks: account-switch recovery, then permission posture.

    Grouped because both read the operator's live Claude session rather than
    teatree's own state, and both must run AFTER ``ensure_django`` — the
    account-switch probe builds messaging backends through the overlay factory to
    live-probe connector reachability once the cache is invalidated. Only the
    account-switch half can hard-FAIL; the permission-mode half advises and its
    return value is deliberately discarded, so it cannot become a gate by accident.
    """
    ok = _check_account_switch()
    _check_interactive_permission_mode()
    return ok


def _check_enabled_but_unprovisioned() -> bool:
    """The "enabled but not provisioned → FAIL" family (epic #3445).

    A dependency the operator ENABLED but nothing installed reads as configured while
    silently doing nothing: a ``review_skill`` naming an absent SKILL.md (#3352), or the
    ``pyright-lsp`` plugin enabled without its ``pyright-langserver`` binary (#3568, the
    LSP never starts). ``_check_declared_dependencies_provisioned`` is the GENERAL gate
    over the same class (#3652) — it enumerates every mandate from the declaration
    surfaces, so a newly mandated skill / binary / integration is covered with no change
    here. All HARD-FAIL, and each runs independently (no short-circuit) so every finding
    is emitted; returns their AND. The review-skill check reads the ConfigSetting store,
    so the caller runs this after :func:`ensure_django`.
    """
    declared = _check_declared_dependencies_provisioned()
    review_skills = _check_configured_review_skills()
    pyright_lsp = _check_pyright_lsp_plugin()
    return declared and review_skills and pyright_lsp


def _run_daily_advisories() -> None:
    """Post-ensure_django surfacing-only daily advisories — never gate the exit code.

    The idle-time dream-distiller staleness alarm (#1933) + its transcript-
    visibility companion, the compose output-root pin check (#3641), and the Plan-2
    Wave B reconciliation ledger — a daily set of end-to-end outcome assertions (park
    spin, cost-per-delivery, dead-ticket spend, loop freeze, vacuous eval gates, halt
    count, open-question age, duplicate execution) checked against production telemetry
    and DM'd loud to the owner via the notify seam under a per-day idempotency key (so
    the watchdog's frequent doctor runs fire at most one DM per finding per day). All
    read the ORM, so this runs after ``ensure_django``; every one is surfacing-only, so
    its return value is deliberately discarded and none can redden the exit code.
    """
    _check_dream_staleness()
    _check_dream_transcript_visibility()
    _check_compose_output_root_pinned()
    _check_reconciliation_ledger()


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

    # Required tools are no longer a list here: they are declared in pyproject's
    # [tool.teatree.provisioning] required_binaries and gated by the general
    # provisioning check inside _check_enabled_but_unprovisioned below (#3652).
    ok = True
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
    # `--repair` re-points it via
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
    # #3352 + #3568: the "enabled but not provisioned → FAIL" gates (epic #3445) —
    # a configured-but-absent review skill, or the pyright-lsp plugin enabled without
    # its `pyright-langserver` binary (the LSP then silently never starts). Runs after
    # ensure_django() above: the review-skill check reads the ConfigSetting store.
    ok = _check_enabled_but_unprovisioned() and ok
    ok = check_worktree_health() and ok
    ok = _check_single_db() and ok
    ok = _check_control_db_agreement() and ok
    ok = _check_stale_uv_venv() and ok
    ok = _check_stale_path_t3() and ok
    ok = _check_agent_session_pins() and ok
    # #3499: the hooks read settings through a DIFFERENT interpreter than the CLI, so a
    # store the CLI reads fine can be unreadable to every cold-hook gate. Runs after
    # ensure_django() above: it compares the hook's answer against the Django-side one.
    ok = _check_cold_hook_settings_readable() and ok
    # Verify the Claude Code statusLine block (PR-17: present, absolute path, executable
    # target — a missing block WARNs, a relative/non-executable one hard-FAILs) AND its
    # freshness. The freshness backstop hard-FAILs a pre-rendered statusline gone stale past
    # the readers' own cutoff while autoload is on — a headless render chain that stopped
    # keeping the file fresh, never an unnoticed regression. Both run unconditionally (the
    # ``all`` tuple calls both before short-circuiting) so a config FAIL never masks a
    # freshness FAIL; the freshness read of the ConfigSetting ``autoload`` flag runs after
    # the ensure_django() above.
    ok = all((check_statusline(), check_statusline_freshness())) and ok

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

    # The ORM-reading loop/intent advisories, grouped so run_doctor_checks stays lean.
    # Runs post-ensure_django (all read the ORM); returns the intent-freshness gating
    # verdict for the caller's `ok` aggregation.
    ok = _run_loop_intent_gates() and ok

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

    # Post-ensure_django, surfacing-only daily advisories, grouped to keep
    # run_doctor_checks lean: the dream-distiller staleness/transcript alarms
    # (#1933), the compose output-root pin check (#3641), and the Plan-2 Wave B
    # reconciliation ledger. None gate the exit code.
    _run_daily_advisories()

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

    # Slack engagement (#256): WARN when `autoload` is OFF yet a Slack posting token
    # is configured — with engagement default-off a session never engages teatree, so
    # a configured bot never routes Slack through the MCP tools. Surfacing-only (never
    # gates the exit code): `autoload` off is a legitimate colleague/opted-out posture.
    check_slack_engagement()

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
