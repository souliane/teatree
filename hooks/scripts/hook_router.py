#!/usr/bin/env python3
"""Unified hook router — single Python process for all Claude Code lifecycle hooks.

Replaces five bash scripts that each spawned bash + jq per invocation.
In a 200-tool-call session with 3 hooks per call, this eliminates ~600
subprocess spawns.

Usage in hooks.json::

    "command": "python3 ${CLAUDE_PLUGIN_ROOT}/hooks/scripts/hook_router.py --event <EVENT>"

Reads JSON from stdin. Writes JSON to stdout when blocking (PreToolUse deny).
Exits 0 silently for passthrough.
"""

import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess  # noqa: S404 — stdlib subprocess for trusted internal git/CLI calls
import sys
import tempfile
import time
import traceback
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from types import ModuleType

# When run as a script (the live hook: ``python3 .../hooks/scripts/hook_router.py``)
# the plugin root — the directory that CONTAINS ``hooks/`` — is not on ``sys.path``,
# so the absolute ``hooks.scripts.*`` package imports below would not resolve. Add
# it once, so the SAME canonical import paths work whether the router runs as a
# script or is imported as ``hooks.scripts.hook_router`` (tests). This replaces the
# former dual-mode hack (scripts-dir-on-path + a ``sys.modules['hook_router']``
# alias): a single package root means one canonical module object per name, so a
# test patching ``hooks.scripts.hook_router.STATE_DIR`` reaches exactly what a
# handler reads — no alias needed.
if str(Path(__file__).resolve().parents[2]) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
# When run as a script the module loads under ``__main__``; register it under its
# canonical package name too, so a handler's ``from hooks.scripts.hook_router
# import <live-global>`` resolves to THIS running instance (whose ``main`` stamps
# the per-call ``_CURRENT_DATA``) rather than a fresh re-import with empty state.
# The shared mutable per-call state is extracted to ``hooks.scripts.hook_context``
# (never ``__main__``); this line only bridges the router's own remaining globals.
if __name__ == "__main__":
    sys.modules.setdefault("hooks.scripts.hook_router", sys.modules[__name__])

from hooks.scripts.answer_first_gate import handle_answer_first_gate
from hooks.scripts.banned_terms import handle_banned_terms_pretool
from hooks.scripts.bash_env import resolve_loop_env as _resolve_loop_env
from hooks.scripts.classifier_relax_gate import (
    _SETTINGS_JSON_PATH,  # noqa: F401 — re-export for test access
    _ask_question_has_relax_option,  # noqa: F401 — re-export for test access
    _block_is_settings_write,  # noqa: F401 — re-export for test access
    _settings_json_target,  # noqa: F401 — re-export for test access
    handle_allow_classifier_relax_settings_write,
)
from hooks.scripts.completion_claim_gate import handle_completion_claim_gate
from hooks.scripts.config_overwrite_guard import handle_block_config_overwrite
from hooks.scripts.coverage_gate import coverage_finding_for_command as _coverage_finding_for_command
from hooks.scripts.coverage_gate import is_merge_class_command as _is_merge_class_command
from hooks.scripts.cron_tracking import (
    cron_cadence_seconds as _cron_cadence_seconds,  # noqa: F401 re-export for test access
)
from hooks.scripts.cron_tracking import derive_loop_name as _derive_loop_name  # noqa: F401 re-export for test access
from hooks.scripts.cron_tracking import handle_track_cron_jobs
from hooks.scripts.deny_circuit_breaker import apply_deny_circuit_breaker as _apply_deny_circuit_breaker
from hooks.scripts.deny_circuit_breaker import (
    deny_circuit_breaker_enabled as _deny_circuit_breaker_enabled,  # noqa: F401 re-export for test access
)
from hooks.scripts.deny_circuit_breaker import (
    deny_circuit_breaker_threshold as _deny_circuit_breaker_threshold,  # noqa: F401 re-export for test access
)
from hooks.scripts.deny_circuit_breaker import (
    deny_is_ux_gate as _deny_is_ux_gate,  # noqa: F401 re-export for test access
)
from hooks.scripts.deny_circuit_breaker import reset_deny_streak as _reset_deny_streak
from hooks.scripts.direct_command_guard import (
    BLOCKED_COMMANDS as _BLOCKED_COMMANDS,  # noqa: F401 re-export for test access
)
from hooks.scripts.direct_command_guard import deny_match as _deny_match  # noqa: F401 re-export for test access
from hooks.scripts.direct_command_guard import handle_block_direct_commands
from hooks.scripts.dispatch_admission_gate import handle_dispatch_admission
from hooks.scripts.dispatch_ledger import handle_track_agents
from hooks.scripts.dispatch_seat_release import handle_subagent_stop_release
from hooks.scripts.django_bootstrap import bootstrap_teatree_django
from hooks.scripts.engagement import autoload_skill_demand, engage
from hooks.scripts.engagement_advisory import session_start_advisory as _session_start_advisory
from hooks.scripts.forge_api_detect import (
    _API_CREATE_ENDPOINT_RE,  # noqa: F401 re-export for test access
    _GLAB_GH_API_RE,
    _MERGE_ENDPOINT_RE,  # noqa: F401 re-export for test access
    _REVIEW_POST_BODY_FLAG_RE,  # noqa: F401 re-export for test access
    _REVIEW_POST_METHOD_RE,  # noqa: F401 re-export for test access
    _effective_method_is_write,  # noqa: F401 re-export for test access
)
from hooks.scripts.gate_result import (
    GateOutcome,
    GateSkipped,
    ValidatorTimedOut,
    classify_validator_run,
    validator_timeout_seconds,
    warn_gate_skipped,
    warn_validator_timed_out,
)
from hooks.scripts.git_add_all_guard import handle_block_git_add_all
from hooks.scripts.glab_stale_base_remote_guard import handle_block_glab_stale_base_remote
from hooks.scripts.handlers.classifier_denial import (
    handle_classifier_deny_stop_gate,
    handle_clear_classifier_deny_marker,
    handle_track_classifier_denial,
)
from hooks.scripts.headless_authoring_gate import handle_block_interactive_authoring
from hooks.scripts.loop_owner_db import db_lease_consult_disabled as _db_lease_consult_disabled
from hooks.scripts.loop_owner_db import db_owner_is_current_session as _db_owner_is_current_session
from hooks.scripts.loop_prompt_shape import LOOP_PROMPT as _LOOP_PROMPT  # noqa: F401 re-export for sibling + tests
from hooks.scripts.loop_prompt_shape import is_bare_loop_prompt as _is_bare_loop_prompt
from hooks.scripts.loop_registrations import emit_loop_registrations, emit_standing_directives_once
from hooks.scripts.loop_registry_liveness import pid_namespace as _pid_namespace
from hooks.scripts.loop_registry_liveness import prune_dead_owner as _prune_dead_owner
from hooks.scripts.loop_state_self_pump_gate import db_loop_state_suppresses_self_pump
from hooks.scripts.main_clone_guard import handle_block_main_clone_mutation
from hooks.scripts.managed_repo import cwd_teatree_managed_state as _cwd_is_teatree_managed
from hooks.scripts.managed_repo import file_is_inside_worktree as _file_is_inside_worktree
from hooks.scripts.managed_repo import is_agent_state_path as _is_agent_state_path
from hooks.scripts.managed_repo import load_protected_branches as _load_protected_branches
from hooks.scripts.managed_repo import overlay_managed_repo_signals as _overlay_managed_repo_signals
from hooks.scripts.managed_repo import repo_root_is_teatree_managed as _repo_root_is_teatree_managed
from hooks.scripts.managed_repo import resolve_branch_and_root as _resolve_branch_and_root
from hooks.scripts.managed_repo import teatree_src_on_path as _teatree_src_on_path
from hooks.scripts.mcp_slack_write_guard import handle_block_mcp_slack_write, is_slack_mcp_tool
from hooks.scripts.memory_recall import handle_recall_cold_memory
from hooks.scripts.merged_detection_probe_gate import handle_warn_merged_detection_probe
from hooks.scripts.mr_cli_fields import (
    cli_update_is_title_only,
    extract_api_mr_fields,
    extract_cli_mr_fields,
    extract_mr_target_repo,
    merge_target_managed_state,
)
from hooks.scripts.mr_validator import mr_validate_argv, run_mr_validator
from hooks.scripts.no_self_reviewer_assign import handle_block_self_reviewer_assign
from hooks.scripts.orchestration_boundary_signals import PYTEST_VERB_FINDER as _PYTEST_VERB_FINDER
from hooks.scripts.orchestration_boundary_signals import PYTEST_VERB_RE as _PYTEST_VERB_RE
from hooks.scripts.orchestration_boundary_signals import call_is_from_subagent as _call_is_from_subagent
from hooks.scripts.orchestrator_investigation_gate import handle_enforce_orchestrator_investigation_boundary
from hooks.scripts.plan_edit_gate import (  # noqa: F401 re-export
    _resolve_worktree_state,
    _ticket_state_for_cwd,
    handle_block_edit_before_planned,
    skip_plan_gate_token,
)
from hooks.scripts.question_gates import (
    FENCED_CODE_RE,
    STRUCTURED_QUESTION_BLOCK,
    denied_question_dedupe_key,
    denied_question_row_marker,
    handle_resolve_answered_question,
    handle_warn_batched_questions,
    is_user_directed_question,
    preceding_user_rejected_question_and_asked_clarify,
)
from hooks.scripts.question_gates import last_assistant_turn as _last_assistant_turn
from hooks.scripts.question_gates import read_transcript_entries as _read_transcript_entries
from hooks.scripts.quote_scanner_verdict_io import quote_scanner_high_block_message as _quote_scanner_high_block_message
from hooks.scripts.quote_verdict import resolve_high_verdict as _resolve_quote_verdict
from hooks.scripts.raw_pid_kill_guard import handle_block_raw_pid_kill
from hooks.scripts.raw_review_post_guard import (
    REVIEW_POST_ENDPOINT_RE as _REVIEW_POST_ENDPOINT_RE,  # noqa: F401 re-export for test access
)
from hooks.scripts.raw_review_post_guard import handle_block_raw_review_post
from hooks.scripts.raw_review_post_guard import (
    is_raw_review_write as _is_raw_review_write,  # noqa: F401 re-export for test access
)
from hooks.scripts.resume_admission import handle_subagent_stop_track_agent, resume_admission_advisory
from hooks.scripts.secret_file_print_guard import handle_block_secret_file_print
from hooks.scripts.self_dm_destinations import SelfDmDestinations as _SelfDmDestinations
from hooks.scripts.self_dm_destinations import read_self_dm_destinations as _read_self_dm_destinations
from hooks.scripts.self_dm_destinations import self_dm_destination as _self_dm_destination
from hooks.scripts.self_dm_destinations import slack_tool_suffix as _slack_tool_suffix
from hooks.scripts.session_end_work_check import handle_session_end
from hooks.scripts.session_handover_pickup import claim_session_handover as _claim_session_handover
from hooks.scripts.session_nudges import handle_todo_freshness_nudge
from hooks.scripts.session_start_skills import session_start_skill_context as _session_start_skill_context
from hooks.scripts.single_branch_repo_guard import handle_block_second_branch
from hooks.scripts.skill_loader_input import build_skill_loader_input as _build_skill_loader_input
from hooks.scripts.skill_path_probe import is_file_safe
from hooks.scripts.skill_suggestion_render import render_skill_suggestion_message
from hooks.scripts.slack_mirror_wiring import build_dm_audio_enricher
from hooks.scripts.slack_mirror_wiring import slack_http_poster as _slack_http_poster
from hooks.scripts.standing_goal_stop_gate import handle_standing_goal_stop
from hooks.scripts.state_files import append_line, read_lines
from hooks.scripts.stop_snapshot_slot import handle_stop_snapshot_slot
from hooks.scripts.stop_snapshot_slot import open_prs_for_repo as _open_prs_for_repo
from hooks.scripts.stop_snapshot_slot import render_git_state_section as _render_git_state_section
from hooks.scripts.stop_snapshot_slot import run_prepare_stop_best_effort as _run_prepare_stop_best_effort
from hooks.scripts.subagent_hint import suppress_self_auth_hint_for_subagent as _suppress_self_auth_hint_for_subagent
from hooks.scripts.subagent_no_commit import handle_subagent_stop_no_commit
from hooks.scripts.task_created_deny import emit_task_create_deny
from hooks.scripts.teatree_settings import autoload_enabled as _autoload_enabled
from hooks.scripts.teatree_settings import teatree_bool_setting as _teatree_bool_setting
from hooks.scripts.teatree_settings import teatree_bool_setting_loud as _teatree_bool_setting_loud
from hooks.scripts.teatree_settings import teatree_int_setting as _teatree_int_setting
from hooks.scripts.turn_inspect import current_turn_assistant_text as _current_turn_assistant_text
from hooks.scripts.turn_inspect import current_turn_edits as _current_turn_edits
from hooks.scripts.turn_inspect import current_turn_tool_commands
from hooks.scripts.unbacked_claim_gate import handle_unbacked_claim_gate
from hooks.scripts.unbounded_wait_guard import handle_block_unbounded_wait
from hooks.scripts.unknown_repo_push_gate import handle_block_unknown_repo_push
from hooks.scripts.ups_fastpath import has_pending_chat_work, has_pending_question_work, record_presence
from hooks.scripts.verbatim_paste_gate import handle_block_verbatim_operator_paste, handle_record_operator_message

STATE_DIR = Path(
    os.environ.get(
        "TEATREE_CLAUDE_STATUSLINE_STATE_DIR",
        os.environ.get("T3_HOOK_STATE_DIR", "/tmp/claude-statusline"),  # noqa: S108 — fixed agent-controlled path, not user input
    )
)

# Per-invocation context shared with the deny circuit breaker. Each hook event
# is a fresh ``python3`` process (one per tool call), so these globals are set
# once by ``main`` and never carry across calls. The breaker reads them so the
# centralised ``emit_pretooluse_deny`` chokepoint can fingerprint a deny against
# the session without threading ``data`` through 15+ existing call sites.
_CURRENT_EVENT: str = ""
_CURRENT_DATA: dict = {}


def _current_hook_context() -> tuple[str, dict]:
    """The per-process hook ``(event, payload)`` ``main`` stamps once per call.

    The deny circuit breaker (extracted to ``deny_circuit_breaker``) reads this
    seam instead of reaching into the router's runtime globals directly.
    """
    return _CURRENT_EVENT, _CURRENT_DATA


_FILE_PATH_TOOLS = {"Read", "Edit", "Write"}
_PATH_TOOLS = {"Grep", "Glob"}
_MR_TOOLS = {"mcp__glab__glab_mr_create", "mcp__glab__glab_mr_update"}

# Patterns that indicate workspace/infrastructure operations where the agent
# MUST use `t3` CLI instead of running underlying commands directly.
_T3_CLI_REMINDER_RE = re.compile(
    r"\b("
    r"worktree|setup|workspace|database|restore|migrate|runserver|"
    r"manage\.py|nx serve|docker compose|createdb|dropdb|"
    r"playwright|e2e|frontend|backend|dslr|pg_restore|pg_dump|"
    r"npm run|pipenv|pip install"
    r")\b",
    re.IGNORECASE,
)

_T3_CLI_REMINDER = (
    "MANDATORY: Use `t3` CLI for ALL workspace, server, database, and test operations. "
    "NEVER run underlying commands directly (manage.py, nx serve, docker compose, "
    "createdb, playwright, npm run, pipenv, pip install, dslr, etc.). "
    "If a `t3` command fails, fix the `t3` code — do not work around it."
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Unified hook router")
    parser.add_argument("--event", required=True, help="Hook event name")
    return parser.parse_args()


# Per-session state files (``<session>.skills`` / ``.agents`` / ``.crons`` …)
# are never cleaned up when a session ends, so the state dir accumulates
# hundreds of stale files over time (#130). A throttled mtime sweep removes
# anything older than the retention window. The throttle sentinel keeps the
# sweep from walking the directory on every single state write — it runs at
# most once per ``_SWEEP_THROTTLE_SECONDS``.
_STATE_FILE_MAX_AGE_SECONDS = 2 * 24 * 60 * 60
_SWEEP_THROTTLE_SECONDS = 60 * 60
_SWEEP_SENTINEL = ".last-sweep"

# Suffixes the sweep must never delete by age, because a live reader gates
# behaviour on the file's presence AND the file's mtime does not refresh for
# the life of an active session. ``.crons`` is written once by
# ``handle_track_cron_jobs`` at registration and then read on every prompt by
# the statusline (which derives readable loop names from the tracked cron/loop
# jobs); an active long-lived session that never changes its crons keeps an
# unmodified ``.crons`` that ages past the retention window. Sweeping it would
# blank the statusline's loop-name display for a session that is already running
# the loop. ``.teatree-active`` is the same
# class: it is touched by ``handle_track_skill_usage`` when a teatree-activating
# skill loads — in a normal session that happens at the start and is not
# repeated for the life of the session — and ``statusline.sh`` gates the WHOLE
# statusline on its presence (exits blank when absent). Sweeping it makes a
# long-lived session's statusline silently go blank. The throttle-and-recreate
# markers (``loop-pending`` / ``pump-armed`` / ``mr_refreshed`` …) are NOT
# listed: their absence is the safe default and they are re-armed on demand.
#
# ``.agents`` / ``.agents-stopped`` (#4108) are a THIRD reason to protect, distinct
# from the first two: ``live_restored_agents`` reads them as a SET DIFFERENCE, so
# the pair must age out together or not at all. A long-running session with agents
# still in flight keeps dispatching (refreshing ``.agents``) while nothing has
# terminated in over the retention window (``.agents-stopped`` goes stale) — sweeping
# only the stopped ledger reinstates the WHOLE append-only dispatch history as
# "restored" on the next resume, the exact false-positive the design note in
# ``resume_admission.py`` says the set-difference exists to avoid.
_SWEEP_PROTECTED_SUFFIXES = frozenset({"crons", "teatree-active", "agents", "agents-stopped"})


def _sweep_stale_state_files() -> None:
    """Remove ephemeral state files older than the retention window (throttled).

    Files whose suffix is in ``_SWEEP_PROTECTED_SUFFIXES`` are skipped — they
    are read live by gates whose mtime does not refresh for an active session,
    so age is not a liveness signal for them.

    Best-effort and crash-proof: any OS error is swallowed so a sweep can
    never break the state write it piggybacks on. Throttled via the
    ``_SWEEP_SENTINEL`` mtime so the directory is walked at most once per
    ``_SWEEP_THROTTLE_SECONDS``.
    """
    sentinel = STATE_DIR / _SWEEP_SENTINEL
    now = time.time()
    try:
        if sentinel.is_file() and now - sentinel.stat().st_mtime < _SWEEP_THROTTLE_SECONDS:
            return
        sentinel.write_text("", encoding="utf-8")
        cutoff = now - _STATE_FILE_MAX_AGE_SECONDS
        for entry in STATE_DIR.iterdir():
            if entry.name == _SWEEP_SENTINEL or not entry.is_file():
                continue
            if entry.name.rsplit(".", 1)[-1] in _SWEEP_PROTECTED_SUFFIXES:
                continue
            if entry.stat().st_mtime < cutoff:
                entry.unlink(missing_ok=True)
    except OSError:
        return


def _ensure_state_dir() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    # _sweep_stale_state_files swallows its own OSError, so no guard here.
    _sweep_stale_state_files()


def _read_input() -> dict:
    try:
        return json.loads(sys.stdin.read())
    except (json.JSONDecodeError, OSError):
        return {}


def emit_pretooluse_deny(reason: str, *, gate_id: str | None = None) -> bool:
    """Emit a PreToolUse deny in the modern nested ``hookSpecificOutput`` schema.

    Claude Code 2.1.146 honours deny payloads only when (a) the JSON
    envelope places ``permissionDecision`` inside ``hookSpecificOutput``
    (the modern SDK schema in
    ``claude_agent_sdk.types.PreToolUseHookSpecificOutput``), AND (b)
    the router exits with code 2 (the changelog fix: "Fixed
    ``PreToolUse`` hooks that emit JSON to stdout and exit with code 2
    not correctly blocking the tool call").

    This helper centralises the schema so adding a new deny gate cannot
    drift back to the legacy flat shape. The legacy top-level
    ``permissionDecision`` / ``permissionDecisionReason`` keys are
    written alongside the nested envelope for backward-compat with
    in-process tests that read ``out["permissionDecision"]`` directly.

    The caller still returns ``True`` to short-circuit the handler chain
    in ``main()``; ``main()`` translates that into ``sys.exit(2)``.

    Every deny is routed through the repeated-denial circuit breaker
    (:func:`_apply_deny_circuit_breaker`) so a session that loops on one
    identical gate cannot burn tokens indefinitely: a UX/non-safety gate
    auto-relaxes once (the breaker returns ``False`` here, allowing the
    call), while a safety gate keeps denying with an escalation appended to
    the reason. The breaker is crash-proof and falls back to the original
    deny on any internal error.

    Returns ``True`` so handlers can ``return emit_pretooluse_deny(...)``,
    or ``False`` when the breaker auto-relaxed a UX gate.
    """
    decision = _apply_deny_circuit_breaker(reason)
    if decision.allow:
        return False
    # A sub-agent deny must not advertise the ALLOW_*/QUOTE_OK self-bypass hint it
    # cannot self-authorize (the classifier-denied retry poisoned its context) —
    # rewrite the hint to escalation guidance, deny unchanged (#3252, sibling leaf).
    reason_out = _suppress_self_auth_hint_for_subagent(decision.reason, _current_hook_context()[1])
    return _write_pretooluse_deny(reason_out, gate_id=gate_id)


def _write_pretooluse_deny(reason: str, *, gate_id: str | None = None) -> bool:
    payload = {
        # Legacy flat shape — kept for in-process consumers (existing
        # handler tests). Harmless to the harness because it ignores
        # unknown top-level keys.
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
        # Modern shape — the one the harness actually reads.
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        },
    }
    # A small non-privacy-sensitive gate identity a gate can stamp on its deny
    # (PR-25 plan_gate marker) so the transcript-conformance eval can key on the
    # gate WITHOUT ever reading the raw (privacy-sensitive) deny reason.
    if gate_id:
        payload["gate_id"] = gate_id
        payload["hookSpecificOutput"]["gate_id"] = gate_id
    json.dump(payload, sys.stdout)
    return True


# ── Shared fail-open / self-rescue routing for the OVER-DENY gates ──
#
# The OVER-DENY gates (skill-loading, protect-default-branch, validate-mr
# broken-env, block-uncovered-diff, agent-plan-gate, and the PRIVATE-surface
# quote/banned downgrade) can wedge the factory when their detection
# misbehaves. They route every deny through ``_fail_open_or_deny`` so two
# always-available escapes apply uniformly:
#
# * a SELF-RESCUE command (``t3 <overlay> gate disable``, ``db migrate``,
#   ``t3 review gate fail-open enable``) is NEVER denied — no gate may block
#   the very commands that rescue a lockout (#1472/#1474 deadlocked twice);
# * with the master ``[teatree] danger_gate_fail_open`` switch ON, every
#   over-deny gate flips to fail-open at once.
#
# The HARD INVARIANT (regression-guarded in test_public_leak_gate_*): the
# PUBLIC-egress leak path (quote/banned on a PUBLIC surface,
# ``publish_surface`` carve-out) MUST NEVER call this helper and MUST NEVER
# read ``danger_gate_fail_open`` — it stays fail-CLOSED always. Relaxing a
# public leak block is a privacy regression, not a lockout rescue.
#
# Both resolvers fail CLOSED to ENFORCEMENT (deny): a broken import or a
# raising resolver must never silently relax a gate. This is the OPPOSITE of
# the gates' own broken-env posture, because THIS helper is the relax path.


def _bootstrap_teatree_src() -> "tuple[ModuleType, ModuleType] | None":
    """Import the self-rescue + fail-open resolvers from the sibling ``src/``.

    The hook runs in the user's session shell with no guarantee ``teatree``
    is importable (#1314), so ``src/`` is bootstrapped onto ``sys.path``.
    Returns ``(self_rescue, teatree_gate)`` modules, or ``None`` on any
    import failure — the caller then fails CLOSED (deny).
    """
    src_dir = Path(__file__).resolve().parents[2] / "src"
    added = False
    try:
        if str(src_dir) not in sys.path:
            sys.path.insert(0, str(src_dir))
            added = True
        from teatree.cli import teatree_gate  # noqa: PLC0415 — deferred: cold-hook import after sys.path setup
        from teatree.hooks import self_rescue  # noqa: PLC0415 — deferred: cold-hook import after sys.path setup
    except Exception:  # noqa: BLE001 — crash-proof hook: any failure degrades silently, never breaks the tool call
        return None
    finally:
        if added:
            with contextlib.suppress(ValueError):
                sys.path.remove(str(src_dir))
    return self_rescue, teatree_gate


def _is_self_rescue(command: str) -> bool:
    """True iff ``command``'s first segment is an always-allowed self-rescue command.

    Fails CLOSED to "not a rescue" (return ``False``) on any import/resolution
    error so a broken environment cannot fabricate a rescue verdict that
    bypasses a gate.
    """
    if not command:
        return False
    modules = _bootstrap_teatree_src()
    if modules is None:
        return False
    self_rescue, _ = modules
    try:
        return bool(self_rescue.is_self_rescue(command))
    except Exception:  # noqa: BLE001 — crash-proof hook: any failure degrades silently, never breaks the tool call
        return False


def _danger_gate_fail_open_enabled() -> bool:
    """True iff the master ``[teatree] danger_gate_fail_open`` switch is ON.

    Fails CLOSED to disabled (return ``False``) on any import/resolution
    error so a broken environment never silently relaxes every gate.
    """
    modules = _bootstrap_teatree_src()
    if modules is None:
        return False
    _, teatree_gate = modules
    try:
        return bool(teatree_gate.danger_gate_fail_open_is_enabled())
    except Exception:  # noqa: BLE001 — crash-proof hook: any failure degrades silently, never breaks the tool call
        return False


def _fail_open_or_deny(data: dict, reason: str, *, gate_id: str | None = None) -> bool:
    """Deny with ``reason`` unless a self-rescue command or fail-open says allow.

    The single chokepoint every OVER-DENY gate routes its deny through. A
    self-rescue command is always allowed; an enabled master fail-open switch
    allows everything; otherwise the deny is emitted. Returns ``True`` (deny
    emitted) or ``False`` (allow), so callers ``return _fail_open_or_deny(...)``.
    ``gate_id`` stamps the optional non-privacy gate marker on the deny output.

    NEVER call this from the PUBLIC-egress leak path — that path stays
    fail-closed (see the module note above).
    """
    try:
        command = data.get("tool_input", {}).get("command", "") if data.get("tool_name") == "Bash" else ""
        if _is_self_rescue(command):
            return False
        if _danger_gate_fail_open_enabled():
            return False
    except Exception:  # noqa: BLE001 — a raising resolver must NEVER relax a gate; fail CLOSED to deny.
        return emit_pretooluse_deny(reason, gate_id=gate_id)
    return emit_pretooluse_deny(reason, gate_id=gate_id)


def _state_file(session_id: str, suffix: str) -> Path:
    return STATE_DIR / f"{session_id}.{suffix}"


def _teatree_active(session_id: str) -> bool:
    if not session_id:
        return False
    return _state_file(session_id, "teatree-active").is_file()


def _t3_engaged(session_id: str) -> bool:
    # #256 Option-1 marker: any ``t3:`` skill loaded this session engages the
    # SUGGESTER (set by :func:`handle_track_skill_usage`). Distinct from
    # ``.teatree-active`` — the loop machinery still consults only that one, so a
    # plain lifecycle skill never arms loops.
    return bool(session_id) and _state_file(session_id, "t3-engaged").is_file()


def _teatree_engaged(session_id: str) -> bool:
    # #256 engagement seam: teatree is engaged when the owner enabled autoload,
    # a teatree-requiring skill was loaded (``.teatree-active``), OR any ``t3:``
    # skill was loaded (``.t3-engaged``). Gates the suggester + T3 CLI reminder.
    return _autoload_enabled() or _teatree_active(session_id) or _t3_engaged(session_id)


def _loop_auto_load_active(session_id: str) -> bool:
    """Whether this session may auto-arm the loop/statusline machinery (#256).

    The single gate every session-start auto-load injection point shares —
    the reactive-loop registration (:func:`handle_enforce_loop_on_prompt`) and the
    tick-owner bootstrap (:func:`handle_session_start_bootstrap`). Two conditions
    must BOTH hold:

    - the session opted into teatree (:func:`_teatree_active` — a teatree
        skill was loaded), AND
    - the operator enabled autoload (:func:`_autoload_enabled`).

    ``autoload`` is the ONE owner flag (``[teatree] autoload = true``, or the
    ``T3_AUTOLOAD`` env): it both ENGAGES the session and ARMS its loops. It
    defaults OFF so a colleague who merely clones the repo (and even loads a
    teatree skill) is never nagged to register a cron or shown the loop
    statusline.
    """
    return _teatree_active(session_id) and _autoload_enabled()


def _is_teatree_skill(name: str) -> bool:
    normalized = normalize_skill_name(name)
    return normalized in {"t3:interactive", "interactive"}


def _bare_skill_segment(name: str) -> str:
    """The skill index's key form: the bare segment after a namespace prefix.

    ``build_requires_index`` keys every entry (and its ``requires:`` members)
    by the bare skill-directory name, so a qualified Skill-tool token like
    ``t3:dogfooding`` must be mapped DOWN to ``dogfooding`` to match
    an index entry and resolve its ``requires:`` closure.
    """
    return name.rstrip("/").removesuffix("/SKILL.md").rsplit("/", 1)[-1].rsplit(":", 1)[-1]


def _skill_load_activates_teatree(skills: list[str]) -> bool:
    """Does loading *skills* opt the session into teatree (directly or via requires:)?

    Resolves the ``requires:`` closure against a bare-mapped copy of the input
    so a qualified Skill-tool token (``t3:dogfooding``) expands the same as
    its bare InstructionsLoaded spelling — the trigger index is bare-keyed. The
    bare mapping is scoped to this detection only; the recorded ``.skills``
    closure keeps its own resolution + canonicalization contract.
    """
    bare = [_bare_skill_segment(s) for s in skills]
    return any(_is_teatree_skill(s) for s in _resolve_skill_closure(bare))


_read_lines = read_lines
_append_line = append_line


# ── UserPromptSubmit ────────────────────────────────────────────────


def handle_user_prompt_submit(data: dict) -> None:
    """Suggest cwd/overlay-context skills — never a free-text scan of the prompt."""
    session_id = data.get("session_id", "")
    prompt = data.get("prompt", "")
    if not session_id or not prompt:
        return

    _ensure_state_dir()
    pending = _state_file(session_id, "pending")
    pending.write_text("", encoding="utf-8")

    # #256 default-OFF: a session that has not engaged teatree (no autoload, no
    # teatree/t3: skill loaded) gets NO skill suggestion, NO ``.pending`` write,
    # and NO T3 CLI reminder. ``.pending`` stays empty above, so the PreToolUse
    # skill-loading gate never blocks (never-lockout). The owner opts in via
    # ``/t3:interactive`` (or any ``t3:`` skill), or ``[teatree] autoload = true``.
    if not _teatree_engaged(session_id):
        return

    scripts_dir = Path(__file__).resolve().parent.parent.parent / "scripts"
    if not (scripts_dir / "lib" / "skill_loader.py").is_file():
        return

    loader_input = _build_skill_loader_input(prompt, session_id)

    sys.path.insert(0, str(scripts_dir))
    try:
        from lib.skill_loader import suggest_skills  # noqa: PLC0415 — deferred: cold-hook import after sys.path setup

        result = suggest_skills(loader_input)
    except Exception:  # noqa: BLE001 — crash-proof hook: a broken suggester degrades to the standing demand below
        result = {"suggestions": [], "advisory": [], "companions": []}
    finally:
        sys.path.pop(0)

    # ``autoload`` is a STANDING opt-in, so the platform-skill demand it implies
    # must not depend on the suggester surviving — nor on the overlay metadata
    # the suggester needs. A silently degraded suggester is precisely what made
    # "teatree is on" indistinguishable from "the owner never opted in".
    result["suggestions"] = [*autoload_skill_demand(loader_input["loaded_skills"]), *result.get("suggestions", [])]

    # Deterministic t3 CLI reminder — injected when prompt matches
    # workspace/infrastructure patterns, regardless of skill suggestions.
    t3_reminder = _T3_CLI_REMINDER if _T3_CLI_REMINDER_RE.search(prompt) else ""
    message = render_skill_suggestion_message(
        result, pending=pending, t3_reminder=t3_reminder, normalize=normalize_skill_name
    )
    if message:
        print(message)  # noqa: T201 — hook stdout is the UserPromptSubmit message channel


# ── UserPromptSubmit: live-presence heartbeat (#58 away-misclassification) ────


def handle_record_presence(data: dict) -> None:
    """Stamp a live-presence heartbeat — a prompt proves the user is here.

    ``core.mode_resolution`` reads this stamp to upgrade a schedule-derived mode to
    the configured presence-upgrade mode: a user actively submitting prompts is
    demonstrably here, so the loops must not stay masked off just because the clock
    is outside their configured work hours.
    Fail-open and silent — an unwritable heartbeat never blocks the prompt.
    """
    prompt = data.get("prompt")
    if not prompt:
        return
    # A PURE loop-tick continuation is autonomous, not user presence — stamping
    # it would let the #189 live-turn predicate mistake an owner-session tick for
    # a fresh keystroke, and it is not evidence the user is at the keyboard for
    # the 15-min schedule upgrade either. Skip it on both counts.
    #
    # But suppress ONLY the bare tick, never a prompt that merely *starts with*
    # the loop text (#2155): when the user types a genuine fresh prompt while the
    # owner session is self-pumping, the harness delivers it PREFIXED by the loop
    # continuation text. A `startswith` guard swallowed that live keystroke, so
    # the next AskUserQuestion deferred to a DeferredQuestion even though the user
    # was demonstrably present. `_is_bare_loop_prompt` strips the harness ambient
    # blocks and suppresses only when nothing but the loop prompt remains —
    # genuine user content beyond it proves presence and must stamp.
    if _is_bare_loop_prompt(prompt):
        return
    # Write the heartbeat in pure stdlib — the write never needed Django (the
    # module import did), so a live-presence stamp no longer boots django.setup()
    # on every user prompt (#22). Byte-identical to ``PresenceHeartbeat.record``.
    try:
        record_presence(str(data.get("session_id", "")))
    except Exception:  # noqa: BLE001 — heartbeat is best-effort; never block the prompt.
        return


# ── UserPromptSubmit + PreToolUse: enforce-loop-registration ──────────

_LOOP_CADENCE_DEFAULT = 720


def _loop_cadence_seconds() -> int:
    """Resolve the loop cadence the same way ``t3 loop`` does (#1036).

    Routes through the shared ``teatree.config.cadence_seconds()`` resolver
    (``T3_LOOP_CADENCE`` env first, then the DB-home ``loop_cadence_seconds``
    setting) so the hook's tick-staleness window and the loop-registration cron
    minutes can never diverge from the real slot cadence. Best-effort: if
    ``teatree`` is not importable in this hook process, fall back to the env-only
    read.
    """
    try:
        with _teatree_src_on_path():
            from teatree.config import cadence_seconds  # noqa: PLC0415 — deferred: cold-hook import

            return cadence_seconds()
    except Exception:  # noqa: BLE001 — crash-proof hook: any failure degrades silently, never breaks the tool call
        return int(os.environ.get("T3_LOOP_CADENCE", _LOOP_CADENCE_DEFAULT) or _LOOP_CADENCE_DEFAULT)


def _tick_meta_stale() -> bool:
    xdg = os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))
    meta = Path(xdg) / "teatree" / "tick-meta.json"
    if not meta.is_file():
        return True
    cadence = _loop_cadence_seconds()
    age = int(time.time()) - int(meta.stat().st_mtime)
    return age > cadence * 2


def _cleanup_stale_pending(session_id: str) -> None:
    """Remove other sessions' per-session loop markers.

    Sweeps both ``*.loop-pending`` and ``*.pump-armed`` (#758 N1): a
    crashed session would otherwise leave a stale ``pump-armed`` marker
    whose mere presence suppresses a *new* owner session's self-pump
    (the anti-spin check keys on the marker file existing).
    """
    for suffix in ("loop-pending", "pump-armed"):
        for f in STATE_DIR.glob(f"*.{suffix}"):
            if f.stem != session_id:
                f.unlink(missing_ok=True)


def _claim_loop_ownership(session_id: str) -> None:
    """Atomically claim the tick-owner record for *session_id* if unclaimed.

    Risk-6 fix: when teatree is loaded mid-session (after SessionStart was
    gated out), the ownership-claim logic in
    :func:`handle_session_start_bootstrap` never ran.  The first
    UserPromptSubmit after the marker is set calls this to fill the gap.
    No-ops if a live foreign owner already holds the record, or if the
    ``T3_LOOP_DISOWN`` immediate-mitigation knob is truthy.  Durable per-loop
    pause/disable lives in the DB ``LoopState`` tier (``t3 loop pause`` /
    ``disable``); there is no ``[loops] enabled`` toml kill-switch (the dead cold
    arm was dropped — loop control is ``/loops`` + the DB only).  The in-process
    ``T3_LOOP_DISOWN`` knob is the orthogonal immediate-mitigation lever, not a
    loops kill-switch.
    """
    if _resolve_loop_env("T3_LOOP_DISOWN").strip() not in _DISOWN_FALSEY:
        return
    current_pid = os.getppid()
    with _loop_registry_txn() as box:
        registry = _prune_dead_owner(box[0])
        owner = registry.get(_OWNER_LOOP)
        if owner is not None and owner.get("session_id") != session_id:
            # A foreign, still-alive session holds the file registry. The DB is the
            # take-over authority (#2851): when ``t3 loop claim --take-over`` already
            # moved the LIVE DB lease to THIS session, reconcile the stale file
            # registry (fall through to rewrite ``_OWNER_LOOP``) and WIN the claim, so
            # the new owner emits cron registrations. A foreign/unowned DB lease (or a
            # disabled consult) backs off as before — a live foreign owner is never
            # stolen without an explicit DB hand-off.
            if not _db_owner_is_current_session(session_id):
                box[0] = registry
                return
        elif owner is None and _db_live_foreign_owner(session_id, current_pid=current_pid):
            box[0] = registry
            return
        box[0] = _tick_owner_record(session_id, "")


def handle_enforce_loop_on_prompt(data: dict) -> None:
    """On first prompt, the loop OWNER registers the reactive infra ``/loop``s.

    PR-28 retired the per-enabled-DB-loop ``CronCreate`` mirror (the worker owns that
    cadence now), so this emits ONLY the three reactive infra ``/loop <duration>``
    slots (Slack-answer, self-improve, drain-queue) via the bare sibling
    :mod:`loop_registrations`. Fail-open: no reactive slot resolvable emits nothing.
    Emit-once per session, keyed on the ``loop-pending`` marker (also the
    ``_skill_loading_exempt`` bootstrap signal), so a repeated prompt does not re-nag.

    It ALSO delivers the standing directives (#4166) — same sibling, emitted
    BEFORE the owner election because the INJECTED shape reaches every engaged
    session; the sibling gates the self-waking shape itself, per slot.
    """
    session_id = data.get("session_id", "")
    if not session_id:
        return
    emit_standing_directives_once(session_id, sys.stdout)
    if not _loop_auto_load_active(session_id):
        return
    _claim_loop_ownership(session_id)
    # STICKY ELECTION (#2650): only the OWNER registers. A session that did NOT
    # win/hold the tick-owner record (a DIFFERENT live session owns it) registers
    # NOTHING and writes no pending marker — the loser backs off automatically.
    # ``_session_owns_loop`` reads what ``_claim_loop_ownership`` just decided under the
    # flock (file owner + the #1604 ``_pid_is_foreign`` DB cross-check).
    if not _session_owns_loop(session_id):
        return
    _ensure_state_dir()
    _cleanup_stale_pending(session_id)
    pending = _state_file(session_id, "loop-pending")
    if pending.is_file():  # reactive registrations already emitted this session — do not re-nag
        return
    if emit_loop_registrations(sys.stdout):
        pending.write_text("1", encoding="utf-8")


# ── PreToolUse: enforce-skill-loading ───────────────────────────────
#
# The gate blocks Bash/Edit/Write until every suggested-but-unloaded
# skill is loaded. A suggestion lands in ``<session>.pending`` from the
# supplementary keyword config (``$HOME/.teatree-skills.yml``) or from
# lifecycle/intent detection.
#
# Fail-open contract (the lockout class this closes): a config entry can
# map a keyword to a skill NAME that no longer resolves (renamed or
# removed skill — e.g. ``ac-auditing-repos`` after the rename to
# ``ac-reviewing-codebase``). Demanding a skill the ``Skill`` tool cannot
# load ("Unknown skill") would block ALL Bash/Edit/Write for the whole
# session with no in-session self-rescue. So before blocking, the gate
# verifies each required name resolves to a loadable skill; an
# unresolvable name does NOT block — it emits a one-line warning naming
# the stale skill + the config file and is dropped from the demand. Only
# skills that genuinely resolve but are not yet loaded enforce load-first.
#
# Resolution reuses the canonical :func:`_skill_search_dirs` (defined
# below for skill-usage tracking) so the gate scans the SAME dirs the
# loader builds its trigger index from — the repo ``skills/``
# source-of-truth (lifecycle skills) plus the agent install dirs
# (supplementary skills), honouring the ``T3_SKILL_SEARCH_DIRS`` override.
# ``<session>.pending`` carries bare names (lifecycle ``code``/``debug``,
# supplementary ``ac-*``) AND overlay ``skill_path`` values of the shape
# ``skills/<skill>/SKILL.md``; :func:`_skill_resolves` handles both so the
# gate keeps enforcing load-first for a genuinely-installed overlay skill
# while still failing open on a stale name.
#
# ``<session>.skills`` (the loaded set) and ``<session>.pending`` record a
# skill VERBATIM in whatever shape arrived: the ``Skill``-tool ``PostToolUse``
# records the NAMESPACED form (``t3:rules``), the ``InstructionsLoaded``
# event and the loader's pending writer record the BARE form (``rules``).
# The same skill therefore appears under either spelling.
#
# The namespaced name is the IDENTITY; the bare name is a lossy projection
# of it. Conflating distinct skills across namespaces (``t3:review`` vs a
# hypothetical ``other:review``) by stripping the qualifier would be wrong,
# so both the WRITE boundary (the pending writer, :func:`_record_skills`)
# and the ``PreToolUse`` MATCH boundary (:func:`handle_enforce_skill_loading`)
# normalize UP to the
# fully-qualified canonical via :func:`_canonical_skill_token` — a bare name
# owned by this plugin gains its namespace (``rules`` → ``t3:rules``), never
# stripped down to the bare segment. WRITE keeps state clean going forward;
# MATCH stays robust against today's mixed legacy state.
#
# :func:`_canonical_skill_token` is PURE, TOTAL and IDEMPOTENT: it takes the
# resolved ``(owned, namespace)`` snapshot as arguments rather than reading
# the filesystem itself. The MATCH boundary resolves that snapshot ONCE per
# gate invocation and threads it through BOTH the demand side and the loaded
# side, so a flaky directory read can never canonicalize the two sides
# against different snapshots (the silent, environment-dependent
# under/over-match the per-name scan risked). With an EMPTY ``owned`` (the
# scan failed) the canonicalizer degrades to VERBATIM equality: a bare
# ``code`` and a namespaced ``t3:code`` do NOT match. That strict-degrade is
# the SAFE failure mode — it may re-block (recoverable via the kill-switch,
# the per-call ``[skill-load-ok:]`` token, or the deny circuit breaker), but
# it never satisfies a demand for skill B with skill A. Never-lockout is now
# supplied by those off-ramps, so this prefers strict-degrade over the
# original "a missed normalization fails open" rationale.
#
# RESOLUTION (:func:`_skill_resolves`) applies the INVERSE of the promotion arm
# and nothing else: it de-qualifies THIS plugin's own namespace back to the bare
# directory name, and leaves every foreign namespace untouched — see its
# docstring.


def _dequalify_own_namespace(segment: str) -> str:
    """Strip THIS plugin's own ``<namespace>:`` prefix from a bare skill token.

    The exact inverse of :func:`_canonical_skill_token`'s promotion arm. The
    write boundary canonicalizes a plugin-owned bare name UP to
    ``<namespace>:<name>`` before it reaches ``<session>.pending``, and no skill
    directory is named that way — so without this inverse EVERY plugin-owned
    skill in ``pending`` was dropped as unresolvable and the skill-loading gate
    could never enforce one. The de-qualified name must still exist as a real
    skill dir, so nothing resolves that would not have resolved bare, and a
    foreign namespace is returned untouched (and stays unresolvable).
    """
    prefix, _, bare = segment.rpartition(":")
    return bare if prefix and bare and prefix == _plugin_namespace() else segment


def _skill_resolves(name: str, search_dirs: list[Path]) -> bool:
    """True iff *name* resolves to a loadable skill in *search_dirs*.

    Resolution is deliberately CONSERVATIVE: a name resolves only when its
    own skill directory exists VERBATIM. Two shapes reach
    ``<session>.pending``. A bare name (lifecycle ``code``, supplementary
    ``ac-*``) matches ``<dir>/<name>/SKILL.md``. An overlay ``skill_path``
    (``skills/<skill>/SKILL.md``, emitted by the overlay generator) matches
    when the literal path is a file under a search dir (or its parent), or
    when its ``<skill>`` parent-dir name exists as a skill dir.

    A FOREIGN namespace is never ``:``-stripped — stripping would mis-resolve a
    stale ``old:code`` onto an installed bare ``code`` and re-introduce the very
    fail-closed lockout class this gate exists to prevent. Such a name is
    treated as unresolvable (fail open). The PATH-shaped branch is never
    de-qualified either: its segment is a literal DIRECTORY name, so a stale
    ``skills/<ns>:code/SKILL.md`` must not resolve onto a bare ``code``.

    The bare branch de-qualifies THIS plugin's OWN namespace
    (:func:`_dequalify_own_namespace`), which is not a relaxation but the exact
    inverse of :func:`_canonical_skill_token`'s promotion arm.

    Symlinked skill dirs (the common install shape) resolve through
    ``is_file``.
    """
    stripped = name.rstrip("/")
    if stripped.endswith("/SKILL.md"):
        # Path-shaped overlay ``skill_path``: literal path, then the
        # ``<skill>`` parent-dir name — both taken verbatim.
        if any(is_file_safe(d.parent / name) or is_file_safe(d / name) for d in search_dirs):
            return True
        segment = stripped[: -len("/SKILL.md")].rsplit("/", 1)[-1]
    else:
        segment = _dequalify_own_namespace(stripped.rsplit("/", 1)[-1])
    if not segment or segment == "SKILL.md":
        return False
    return any(is_file_safe(d / segment / "SKILL.md") for d in search_dirs)


def _plugin_namespace() -> str:
    """Return this plugin's namespace from its manifest, defaulting to ``t3``.

    The Claude Code Skill tool prefixes a plugin-owned skill with the
    plugin's ``name`` (``.claude-plugin/plugin.json``) — ``rules`` is
    invoked as ``t3:rules``. Read it from the manifest so a renamed plugin
    stays correct; fall back to ``t3`` on any read failure (the hook must
    never crash).
    """
    manifest = Path(__file__).resolve().parents[2] / ".claude-plugin" / "plugin.json"
    try:
        name = json.loads(manifest.read_text(encoding="utf-8")).get("name", "")
    except (OSError, json.JSONDecodeError, AttributeError):
        return "t3"
    return name if isinstance(name, str) and name else "t3"


def _plugin_skills_dirs() -> list[Path]:
    """Directories whose skills this plugin owns (namespaces under its prefix).

    Production: the plugin's own ``skills/`` tree ONLY — never the shared
    agent install dirs (``~/.claude/skills`` carries non-plugin ``ac-*``
    skills that must stay unqualified). Tests point at a fixture tree via
    the ``T3_SKILL_SEARCH_DIRS`` override (the same seam the resolver uses),
    treating the seeded skills as plugin-owned.
    """
    override = os.environ.get("T3_SKILL_SEARCH_DIRS", "")
    if override:
        return [Path(d) for d in override.split(os.pathsep) if d]
    return [Path(__file__).resolve().parents[2] / "skills"]


def _plugin_owned_skills() -> set[str]:
    """Return the bare names of skills owned by this plugin.

    These are the names the Skill tool namespaces under
    :func:`_plugin_namespace`. A bare ``rules`` present here canonicalizes
    to ``<namespace>:rules``; a name absent here (a supplementary ``ac-*``
    installed elsewhere) is left unqualified.
    """
    owned: set[str] = set()
    for skills_root in _plugin_skills_dirs():
        try:
            owned.update(d.name for d in skills_root.iterdir() if (d / "SKILL.md").is_file())
        except OSError:
            continue
    return owned


def _canonical_skill_token(name: str, owned: frozenset[str], namespace: str) -> str:
    """Canonicalize *name* against an explicit ``(owned, namespace)`` snapshot.

    PURE, TOTAL and IDEMPOTENT — ``f(f(x)) == f(x)`` for every input and it
    never raises. It takes the snapshot as arguments rather than reading the
    filesystem, so the demand side and the loaded side of a match always
    canonicalize against the SAME snapshot (no environment-dependent
    asymmetry from a flaky directory scan).

    The bare segment is the final ``/``-segment after stripping a trailing
    ``/`` and a ``/SKILL.md`` suffix. A ``:`` splits it on the LAST colon
    into ``(prefix, bare)``; with no colon ``prefix`` is empty. Then:

    - ``prefix`` non-empty → ``f"{prefix}:{bare}"`` VERBATIM. An already-qualified
    token is a fixed point; a foreign namespace is preserved, so ``other:review``
    can never equal ``t3:review`` and our own ``t3:review`` never collapses to bare.
    - ``prefix`` empty and ``bare in owned`` → ``f"{namespace}:{bare}"`` (a
    plugin-owned bare name is promoted UP to its namespace).
    - else → ``bare`` (a non-owned ``ac-*`` stays bare).

    With ``owned == frozenset()`` (the scan failed) only the promotion arm is
    disabled, so this collapses to VERBATIM equality: ``f("code") == "code"``
    and ``f("t3:code") == "t3:code"`` and the two do NOT match. That
    strict-degrade is the SAFE failure mode — it may re-block (recoverable
    via the kill-switch, the per-call token, or the deny circuit breaker),
    but it NEVER satisfies a demand for skill B with skill A.
    """
    segment = name.rstrip("/").removesuffix("/SKILL.md").rsplit("/", 1)[-1]
    if not segment:
        return ""
    if ":" in segment:
        prefix, bare = segment.rsplit(":", 1)
        if prefix:
            return f"{prefix}:{bare}"
        # Leading-colon ``:bare``: no real prefix; fall through to bare rules.
        segment = bare
        if not segment:
            return ""
    if segment in owned:
        return f"{namespace}:{segment}"
    return segment


def _skill_canon_snapshot() -> tuple[frozenset[str], str]:
    """Resolve the ``(owned, namespace)`` snapshot ONCE for a gate invocation.

    Wraps the fallible owned-set scan so the resolver stays TOTAL: any read
    failure degrades to an empty set, which :func:`_canonical_skill_token`
    treats as strict (verbatim) equality — the safe failure mode.
    """
    try:
        owned = frozenset(_plugin_owned_skills())
    except OSError:
        owned = frozenset()
    return owned, _plugin_namespace()


def normalize_skill_name(name: str) -> str:
    """Resolve a skill *name* UP to its fully-qualified canonical form.

    Thin WRITE-boundary wrapper over :func:`_canonical_skill_token` that
    resolves the ``(owned, namespace)`` snapshot internally — writers
    (the pending writer, :func:`_record_skills`, :func:`handle_track_skill_usage`)
    are not hot, so a per-call snapshot read is fine. The MATCH boundary
    instead resolves ONE snapshot and threads it through both sides via
    :func:`_canonical_skill_token` directly. NOT used for RESOLUTION (see
    :func:`_skill_resolves`).
    """
    owned, namespace = _skill_canon_snapshot()
    return _canonical_skill_token(name, owned, namespace) or name


# Per-call escape mirroring the ``[fg-ok: <reason>]`` precedent of the
# orchestrator-boundary gate: ``[skill-load-ok: <non-empty-reason>]``
# in the CURRENT tool call's command/args unblocks this single Bash/Edit/
# Write, an empty reason rejects. A false skill-trigger can therefore
# never wedge the loop — but a genuine intent match still hard-blocks
# every call that does NOT carry the escape (the #1488 loophole stays
# closed).
_SKILL_LOAD_OK_RE = re.compile(r"\[skill-load-ok:\s*(\S[^\]]*?)\s*\]")


def _skill_load_ok_token(data: dict) -> str | None:
    """Return the reason from a ``[skill-load-ok: <reason>]`` token, else None.

    Scans the current tool call's command/args — for ``Bash`` the
    ``command`` string, for ``Edit``/``Write`` the written text
    (``new_string`` / ``content``) and the ``file_path`` — within the
    first 512 characters of each field (matching :data:`_FG_OK_RE`'s cap)
    so a buried token in a long body does not silently authorise the
    call. An empty reason returns None.
    """
    tool_input = data.get("tool_input", {})
    if not isinstance(tool_input, dict):
        return None
    for field in ("command", "new_string", "content", "file_path"):
        value = tool_input.get(field, "")
        if not isinstance(value, str) or not value:
            continue
        match = _SKILL_LOAD_OK_RE.search(value[:512])
        if not match:
            continue
        reason = match.group(1).strip()
        if reason:
            return reason
    return None


# File suffixes whose Edit/Write is genuine Python/Django source work. A skill
# demand for ``/ac-python`` / ``/ac-django`` is relevant only to these; a
# ``.md`` / ``.yml`` / ``.toml`` / ``.sh`` / prose edit is not, so the gate must
# not fire on it (the over-block this scope closes).
_PYTHON_SOURCE_SUFFIXES: tuple[str, ...] = (".py", ".pyi")

# A Bash command runs Python tooling when its FIRST word (after benign env /
# `cd` prefixes are not in scope here — the heuristic is conservative on the
# leading verb) is a Python interpreter / packaging / lint / type / test
# runner, or it invokes ``manage.py`` / ``setup.py``. Tightly anchored so a
# pure-git / ls / grep / markdownlint command never counts as code work.
_PYTHON_TOOL_RE: re.Pattern[str] = re.compile(
    r"(?:^|[;&|]\s*|\b)(?:"
    r"python[0-9.]*\b|uv\s+run\b|uvx\b|poetry\s+run\b|pipenv\s+run\b|"
    r"pytest\b|ruff\b|ty\s+check\b|ty-check\b|mypy\b|tox\b|"
    r"[\w./-]*manage\.py\b|[\w./-]*setup\.py\b"
    r")"
)


def _skill_gate_targets_code_work(data: dict) -> bool:
    """True iff this tool call is genuine Python/Django code work.

    The skill-loading gate demands ``/ac-python`` / ``/ac-django`` only for
    Python/Django work, so it must fire ONLY when:

    - ``Edit`` / ``Write`` touches a Python source file (``.py`` / ``.pyi``); or
    - ``Bash`` runs Python tooling (python, uv run, pytest, ruff, ty, manage.py).

    It NEVER fires on ``AskUserQuestion`` (or any other tool), nor on a
    markdown / yaml / toml / shell / prose edit, nor on a pure-git or other
    non-Python Bash command. This is the tight-scope alternative to a fuzzy
    hard-block: the gate cannot cleanly separate Python edits from docs/config/
    git work by intent text, so it keys on the concrete target instead.
    """
    tool_name = data.get("tool_name", "")
    tool_input = data.get("tool_input", {})
    if not isinstance(tool_input, dict):
        return False
    if tool_name in {"Edit", "Write"}:
        file_path = tool_input.get("file_path", "")
        if not isinstance(file_path, str):
            return False
        return file_path.endswith(_PYTHON_SOURCE_SUFFIXES)
    if tool_name == "Bash":
        command = tool_input.get("command", "")
        return isinstance(command, str) and bool(_PYTHON_TOOL_RE.search(command))
    return False


def _skill_loading_exempt(session_id: str) -> bool:
    """True when the skill-load gate must NOT fire for this session's code work.

    NEVER-LOCKOUT (#1918): a loop-registration / t3-master bootstrap turn
    routinely surfaces a resolvable intent skill (the bare word ``loops`` is a
    hard intent trigger) in ``<session>.pending`` while doing genuine code work
    during teatree's own Django setup. Blocking that to demand an unrelated
    ``/loops`` load deadlocks the bootstrap. The skill-load gate is a UX nudge,
    not a safety gate, so it exempts the turn — keyed on the SAME short-lived
    ``<session>.loop-pending`` marker the loop gates use (written by
    :func:`handle_enforce_loop_on_prompt`, cleared once the loop registers), so
    there is one source of truth for "this session is mid loop-bootstrap".

    ``.is_file()`` never raises, so a missing/unreadable marker preserves the
    gate (fails to "not exempt"), never crashes — per the hooks crash-proof
    contract.
    """
    return _state_file(session_id, "loop-pending").is_file()


def _skill_loading_gate_enabled() -> bool:
    """Whether the skill-loading gate is enabled (default True).

    Fails OPEN to enabled on a missing/broken config so the gate keeps its
    protective default; an explicit ``false`` is the one-line kill-switch (never
    a code edit). See :func:`_teatree_bool_setting` for the shared bare-boolean
    semantics.
    """
    return _teatree_bool_setting("skill_loading_gate_enabled", default=True)


def handle_enforce_skill_loading(data: dict) -> bool:
    """Block Python/Django code work when *loadable* suggested skills aren't loaded.

    Scoped to genuine code work (:func:`_skill_gate_targets_code_work`): an
    ``Edit``/``Write`` of a ``.py``/``.pyi`` file or a ``Bash`` Python-tooling
    command. It NEVER fires on ``AskUserQuestion``, a docs/config/shell edit, or
    a pure-git Bash command — the over-block this scope closes.

    Fails open on a stale/unresolvable required skill (see the module
    comment above): such a name is warned about, never blocked on. A
    per-call ``[skill-load-ok: <reason>]`` token in the tool's command/
    args is an explicit escape (#1567) so a false trigger can never wedge
    the loop; a genuine intent match still hard-blocks every code-work call
    lacking that token. The ``skill_loading_gate_enabled`` kill-switch
    (``t3 <overlay> gate skill-loading disable``) is the global off-ramp the
    docs have always named for THIS gate (#4216).
    """
    session_id = data.get("session_id", "")
    if (
        not session_id
        or not _skill_gate_targets_code_work(data)
        or _skill_loading_exempt(session_id)
        # Last, because it is the only clause that reads the config store.
        or not _skill_loading_gate_enabled()
    ):
        return False

    pending_lines = _read_lines(_state_file(session_id, "pending"))
    if not pending_lines:
        return False

    owned, namespace = _skill_canon_snapshot()
    loaded_canonical = {
        _canonical_skill_token(s, owned, namespace) for s in _read_lines(_state_file(session_id, "skills"))
    }
    unloaded = [s for s in pending_lines if _canonical_skill_token(s, owned, namespace) not in loaded_canonical]
    if not unloaded:
        return False

    search_dirs = _skill_search_dirs()
    enforceable = [s for s in unloaded if _skill_resolves(s, search_dirs)]
    stale = [s for s in unloaded if s not in enforceable]

    config_path = os.environ.get("T3_SUPPLEMENTARY_SKILLS", str(Path.home() / ".teatree-skills.yml"))
    for name in stale:
        sys.stderr.write(
            f"WARNING: skill-loading gate skipped unresolvable skill '{name}' "
            f"(not found in any skill dir; check the keyword→skill mapping in {config_path}).\n"
        )

    if not enforceable:
        return False

    if reason := _skill_load_ok_token(data):
        sys.stderr.write(f"NOTE: skill-loading gate skipped via [skill-load-ok: {reason}].\n")
        return False

    skill_list = " ".join(f"/{s}" for s in enforceable)
    reason = (
        f"SKILL LOADING ENFORCEMENT: You MUST load these skills first: {skill_list}. "
        "Call the Skill tool for each one BEFORE calling Bash/Edit/Write. "
        "If this is a false trigger, add `[skill-load-ok: <reason>]` to the command/args to proceed."
    )
    return _fail_open_or_deny(data, reason)


def _plan_edit_gate_enabled() -> bool:
    """Whether the plan-edit gate is enabled (default True).

    Fails OPEN to enabled on a missing/broken config so the gate keeps its
    protective default; an explicit ``false`` is the one-line kill-switch
    (``t3 <overlay> gate plan disable``, never a code edit). See
    :func:`_teatree_bool_setting` for the shared bare-boolean semantics.
    """
    return _teatree_bool_setting("plan_edit_gate_enabled", default=True)


# ── PreToolUse: protect-default-branch ─────────────────────────────


def handle_protect_default_branch(data: dict) -> bool:
    """Block Edit/Write on a source file in a teatree-MANAGED protected-branch repo.

    Scoped to the TARGET FILE's own repo, never to the cwd's branch and
    never to "any git repo" (#126). The block fires only when ALL hold:

    1. the tool is ``Edit``/``Write``/``Read`` with a ``file_path``;
    2. the path is NOT agent-harness state (memory / todos / per-project
        state) — those are git-tracked scratch state, never protected
        source, so they are exempt even on ``main``;
    3. the file's enclosing git repo is on a protected branch;
    4. the file genuinely lives inside that repo's working tree;
    5. that repo is teatree-MANAGED (core + the active overlay's
        registered repos) — an unmanaged repo on ``main`` (a dotfiles
        clone, an unrelated project) is NOT this gate's concern.

    Any condition unmet → allow (fail open). A git error, an
    unresolvable repo, or an unclassifiable slug all allow — the
    gate-over-deny class this change closes means uncertainty errs toward
    letting the write through, not blocking it.
    """
    tool_name = data.get("tool_name", "")
    file_path = data.get("tool_input", {}).get("file_path", "")
    # Agent-harness state is never repo source — allow it even on `main`.
    if tool_name not in _FILE_PATH_TOOLS or not file_path or _is_agent_state_path(file_path):
        return False

    resolved = _resolve_branch_and_root(str(Path(file_path).parent))
    if resolved is None:
        return False
    branch, repo_root = resolved

    if (
        branch not in _load_protected_branches()
        or not _file_is_inside_worktree(repo_root, file_path)
        or not _repo_root_is_teatree_managed(repo_root)
    ):
        return False

    return _fail_open_or_deny(
        data,
        f"BLOCKED: file is on protected branch '{branch}' in a teatree-managed repo. "
        "Create a worktree first with `t3 teatree workspace ticket`.",
    )


# ── PreToolUse: validate-mr-metadata ────────────────────────────────

# Every surface an MR title/description can be SET from — the ``glab mr``
# CLI inline/file/dynamic parsing, the REST-API field surface, and the
# TARGET-repo slug parsing — lives in the bare sibling module ``mr_cli_fields``
# (split out for module health); its extractors are imported above. Only the
# gate handler below stays here.


def _extract_mr_fields(data: dict) -> "tuple[str, str] | GateSkipped | None":
    """Return ``(title, description)`` for an MR create/update, else a skip/``None``.

    ``None`` means "not an MR-metadata mutation" — nothing to validate and
    nothing to say. A :class:`GateSkipped` means the command IS an MR mutation
    the gate cannot evaluate, and carries the reason the caller must print — a
    recognised-but-unevaluated call is never allowed through in silence. A
    returned tuple means the command IS an MR mutation and must be validated
    *even if title/description are empty* — an empty/missing title is exactly
    the kind of bad metadata the gate must reject, not silently pass (#119).

    Covers four surfaces so a non-compliant title/description cannot slip onto
    the forge through any of them:

    1.  ``glab mr create/update --title/--description`` (inline quotes), via
        :func:`extract_cli_mr_fields`. ``create`` validates both fields;
        ``update`` validates ONLY the field(s) it sets (a metadata-only
        reviewer/label/state edit is a named skip — never-lockout).
    2.  The same command's file-based / heredoc description
        (``-F``/``--description-file``) — read via :func:`_read_message_file`
        instead of passed through as a falsely-empty string (the slip class: a
        multi-line prose description whose first line was not the
        ``type(scope): … (ticket_url)`` form). A double-quoted ``$(…)``/``$VAR``
        the hook cannot resolve before shell expansion is a named skip, never
        validated as the truncated literal fragment.
    3.  Out-of-band ``glab api``/``gh api`` PUT/POST to an MR/PR endpoint —
        the web-UI-equivalent description edit that bypasses the CLI (this is
        the GitHub PR-create path: ``gh api repos/<o>/<r>/pulls``).
    4.  The ``mcp__glab__glab_mr_create``/``_update`` MCP tools.

    The ``gh pr create/edit`` CLI is intentionally NOT a surface here: it is
    already governed by the AI-signature gate (`handle_block_ai_signature`) in
    the same PreToolUse chain, and double-gating it would let the metadata deny
    preempt that gate's body scan. GitHub PR creation reaches this gate via the
    ``gh api .../pulls`` REST path instead.
    """
    tool_name = data.get("tool_name", "")
    tool_input = data.get("tool_input", {})

    if tool_name == "Bash":
        command = tool_input.get("command", "")
        # ``extract_cli_mr_fields`` detects a REAL ``glab mr create/update``
        # invocation (ignoring the verb embedded in a quoted arg / heredoc body)
        # and returns the fields, a named GateSkipped, or None when it is not a
        # CLI mutation — only the last falls through to the REST-API surface.
        cli_fields = extract_cli_mr_fields(command)
        if cli_fields is not None:
            return cli_fields
        return extract_api_mr_fields(command)

    if tool_name in _MR_TOOLS:
        return tool_input.get("title", ""), tool_input.get("description", "")

    return None


_MR_VALIDATE_BROKEN_ENV_DENY = (
    "Cannot validate MR title/description — the overlay validator "
    "(`t3 tool validate-mr`) is not resolvable. Refusing to create "
    "the MR with unvalidated metadata (fail closed). Fix the environment, or "
    "set T3_MR_VALIDATE_ALLOW_BROKEN_ENV=1 to deliberately bypass."
)

_MR_VALIDATE_BROKEN_ENV_SKIP = (
    "the overlay validator (`t3 tool validate-mr`) is not resolvable in this "
    "environment and T3_MR_VALIDATE_ALLOW_BROKEN_ENV is set, so the fail-closed "
    "deny was deliberately bypassed"
)


def _handle_broken_validate_env(data: dict) -> bool:
    """Decide the gate's action when no validator could be resolved.

    The MR-metadata gate FAILS CLOSED by default (deny): a non-compliant title
    must never reach GitLab just because the env could not validate it. The
    explicit ``T3_MR_VALIDATE_ALLOW_BROKEN_ENV`` opt-in is the per-gate
    self-rescue — and taking it is announced, never mute, so an MR that goes out
    unvalidated says so. The broken-env deny additionally routes through
    :func:`_fail_open_or_deny` so the master ``danger_gate_fail_open`` switch and
    the always-allowed self-rescue commands relax it too (NEVER-LOCKOUT).
    """
    if os.environ.get("T3_MR_VALIDATE_ALLOW_BROKEN_ENV", "").strip().lower() in {"1", "true", "yes"}:
        warn_gate_skipped("MR-metadata", _MR_VALIDATE_BROKEN_ENV_SKIP)
        return False
    return _fail_open_or_deny(data, _MR_VALIDATE_BROKEN_ENV_DENY)


def _mr_validator_verdict(data: dict, result: "subprocess.CompletedProcess[str] | ValidatorTimedOut | None") -> bool:
    """Map a validator run (or its failure to run) to the gate's block decision."""
    if isinstance(result, ValidatorTimedOut):
        warn_validator_timed_out("MR-metadata", result.allowance_seconds)
        return False
    if result is None:
        return _handle_broken_validate_env(data)

    outcome = classify_validator_run(result)
    if outcome is GateOutcome.CANNOT_EVALUATE:
        # The validator RAN but crashed (a traceback, not a clean verdict).
        # Crash ≠ deny (#1528): warn loudly and allow — the remote CI
        # MR-title/description job is the backstop for real non-compliance.
        sys.stderr.write(
            "NOTE: the MR-metadata validator crashed (could not evaluate) — "
            "allowing the MR to proceed (fail-open-with-warn). The remote "
            "MR-title/description CI job remains the backstop.\n"
        )
        return False
    if outcome is GateOutcome.DENY:
        return emit_pretooluse_deny(
            (result.stderr or result.stdout or "").strip() or "MR title/description failed overlay validation."
        )
    return False


def handle_validate_mr_metadata(data: dict) -> bool:
    """Block a non-compliant ``glab mr``/``gh pr`` create/update before it runs.

    Validates by default via the TARGET overlay's ``validate_pr`` (no env-var
    opt-in) so the pre-push gate is always live (#119 Part 3). The MR's TARGET
    repo is parsed from the command and threaded as ``--repo`` so an MR is graded
    against the TARGET overlay's rules, not the cwd overlay's weaker ones. A
    validator that RAN but crashed, and one too SLOW to finish inside its
    allowance, are both CANNOT_EVALUATE — crash ≠ deny (#1528), and neither is a
    timeout: each warns and allows, with the remote CI job as backstop. Only the
    UNRESOLVABLE validator (no ``t3``, no script — the ``None`` broken-env path)
    FAILS CLOSED; the ``T3_MR_VALIDATE_ALLOW_BROKEN_ENV`` opt-in restores
    fail-open there.

    Every outcome in which the gate does NOT actually validate the MR — an
    unresolvable ``$(…)``/``$VAR`` field, an update setting no governed field,
    the broken-env opt-in, a crash, a timeout — emits one loud named line. Only
    "this is not an MR mutation" and a clean PASS are silent, so a mute gate can
    never be mistaken for one that swallowed the call.
    """
    fields = _extract_mr_fields(data)
    if fields is None:
        return False
    if isinstance(fields, GateSkipped):
        warn_gate_skipped("MR-metadata", fields.reason)
        return False
    title, description = fields
    command = data.get("tool_input", {}).get("command", "") if data.get("tool_name") == "Bash" else ""
    target_repo = extract_mr_target_repo(command) if command else None
    # Title-only update: no description touched → skip required-section check (#3254).
    sections_optional = bool(command) and cli_update_is_title_only(command)

    argv = mr_validate_argv()
    if argv is None:
        return _handle_broken_validate_env(data)

    return _mr_validator_verdict(
        data, run_mr_validator(argv, title, description, target_repo, sections_optional=sections_optional)
    )


# ── PreToolUse: block-ai-signature (#836 §17.6 gate 15) ─────────────

_PR_CREATE_TOOLS = {
    "mcp__glab__glab_mr_create",
    "mcp__glab__glab_mr_update",
    "mcp__github__create_pull_request",
    "mcp__github__update_pull_request",
}


def _extract_bash_ai_sig_payload(command: str, cwd: Path | None = None) -> str | None:
    """Return the scannable forge-post body for a Bash command, or ``None``.

    Delegates the "is this a forge post?" decision and the body extraction to
    the SAME canonical command parser the #1213 quote-scanner, #1415
    banned-terms, and #1530 bare-reference gates use
    (:mod:`teatree.hooks._command_parser`). This was previously a second,
    hand-rolled parser (``_AI_SIG_PR_RE`` / ``_AI_SIG_COMMIT_RE`` /
    ``_PR_BODY_FLAG_RE`` / ``_GIT_COMMIT_M_RE``, all now removed) that covered
    only ``gh pr`` / ``glab mr`` and a QUOTED ``--body`` — it missed
    ``gh issue create/comment``,
    ``glab issue note``, ``glab mr note``, and the ``-b``/heredoc/``-d`` body
    forms, so an AI-signature footer leaked on those surfaces (#11, the
    souliane/skills#38 / #1840 / #1845 recurrence). Reusing the shared parser
    closes the whole class at once: :func:`is_publish_command` recognises every
    forge-post command shape (the contiguous-substring catalogue + the
    token-aware ``api`` WRITE / ``git commit`` classifiers), and
    :func:`extract_bash_payload` pulls the body out of every flag form
    (``--body``/``--description``/``--message``/``-b``/``-m``, ``--body-file``/
    ``--file``/``-F``, ``-d``/``--field`` JSON, heredocs).

    ``fail_closed_body_file=False`` keeps this gate's fail-OPEN contract on an
    unreadable / missing / binary body file (an absent body contributes
    nothing rather than a hard-block sentinel) — a broken environment must
    never block a forge post, matching the other t3-shelling hooks.

    The body extraction itself lives in the public
    :func:`teatree.hooks.ai_signature_gate.extract_forge_post_body` so the
    private ``_command_parser`` import stays INSIDE the ``teatree`` package (the
    hook router cannot import a private name from an external module), mirroring
    how ``banned_terms_scanner.extract_publish_payload`` wraps the same parser.
    """
    from teatree.hooks.ai_signature_gate import extract_forge_post_body  # noqa: PLC0415 — deferred: cold-hook import

    return extract_forge_post_body(command, cwd)


def _extract_ai_sig_payload(data: dict) -> str | None:
    """Return the PR-body / commit-message text to scan, else ``None``.

    Covers the full forge-post command class via the shared canonical parser
    (:func:`_extract_bash_ai_sig_payload`): ``gh pr create/edit/comment``,
    ``gh issue create/comment``, ``glab mr create/update/note``,
    ``glab issue create/note``, ``git commit`` (inline ``-m`` and file-based
    ``-F``/``-C``/``--file`` / ``--body-file`` / ``--description``-file —
    the #831 multi-line shape), the ``gh api``/``glab api`` WRITE to a forge
    endpoint, and the MR/PR MCP create/update tools. ``None`` ⇒ not a forge
    post / commit, or (for a file-based arg) a missing/binary file (fail open).
    """
    tool_name = data.get("tool_name", "")
    tool_input = data.get("tool_input", {})

    if tool_name == "Bash":
        return _extract_bash_ai_sig_payload(tool_input.get("command", ""), _resolve_cwd_repo(data))

    if tool_name in _PR_CREATE_TOOLS:
        return tool_input.get("body", "") or tool_input.get("description", "")

    return None


def _ai_sig_scan_argv() -> list[str] | None:
    t3_bin = shutil.which("t3")
    return [t3_bin, "tool", "ai-sig-scan", "-"] if t3_bin else None


# A genuine finding is recognisable by the scanner's well-formed summary
# header ``AI-signature scan: N banned trailer(s)`` (``scripts/
# ai_signature_scan.py`` ``_summary``). The scanner exits 1 on a finding AND
# nonzero on a crash (a missing/unreadable ``-F`` file → typer traceback →
# exit 1, no summary on stdout), so ``returncode != 0`` alone CANNOT tell the
# two apart — keying on the summary line does, mirroring the sibling
# ``coverage_gate.diff_coverage_finding`` structured-stdout discriminator.
_AI_SIG_FINDING_RE = re.compile(r"^AI-signature scan:\s+\d+\s+banned trailer", re.MULTILINE)


#: Bootstrap-crash markers in the scanner subprocess's stderr — a `t3` binary whose
#: CLI import chain loads Django models before setup (no DJANGO_SETTINGS_MODULE in the
#: hook env) crashes with one of these BEFORE the scanner runs, so the gate cannot
#: confirm the body and must fail OPEN (the documented broken-environment posture),
#: never fail closed on an unrelated import bug.
_AI_SIG_BOOTSTRAP_CRASH_MARKERS = (
    "AppRegistryNotReady",
    "ImproperlyConfigured",
    "ModuleNotFoundError",
    "Apps aren't loaded yet",
)


def _ai_sig_finding(stdout: str) -> str | None:
    """Return the finding summary iff *stdout* is a real banned-trailer finding.

    ``None`` ⇒ not a genuine finding: either the clean summary (``AI-signature
    scan: clean``) or a crash/error with no well-formed summary at all. The
    caller maps the three outcomes to DENY-finding / ALLOW / fail-closed-error.
    """
    return stdout.strip() if _AI_SIG_FINDING_RE.search(stdout) else None


def handle_block_ai_signature(data: dict) -> bool:
    """Refuse a forge-post body / commit message carrying an AI-signature trailer.

    Deterministic enforcement of the "No AI Signature on Posts Made on the
    User's Behalf" rule (BLUEPRINT §17.6 gate 15, #836). The rule was prose
    only in /t3:rules and unenforced at the PR-body layer — PR #831 leaked
    the banned trailer, caught only by cold review. This makes it a code
    gate at the same pre-merge layer as the draft-lock and structured-
    question gates.

    Body extraction now reuses the shared canonical command parser
    (``teatree.hooks._command_parser``) so the gate fires for the WHOLE
    forge-post command class — ``gh pr/issue create/edit/comment``,
    ``glab mr/issue create/update/note``, ``git commit``, and every
    ``--body``/``--body-file``/``-F``/``-b``/``-m`` flag form — closing the
    ``gh issue`` / ``glab note`` / unquoted-body gap a hand-rolled regex
    parser left open (#11). The handler bootstraps ``sys.path`` to import
    ``teatree`` from the sibling ``src/`` dir (the hook runs in the user's
    session shell with no guarantee ``teatree`` is importable, #1314) and
    fails open on a broken environment (no ``t3`` / import error / timeout),
    matching the other t3-shelling hooks — a gate that cannot run AT ALL must
    not lock out every commit.

    Three outcomes are kept DISTINCT (#1884), because this is a SECURITY gate
    that prevents publishing AI signatures under the user's identity:
    (a) scanner ran, found a trailer (well-formed ``AI-signature scan: N
    banned trailer(s)`` summary) → DENY with the finding message;
    (b) scanner ran, clean → ALLOW;
    (c) scanner WAS invoked but exited nonzero with no well-formed finding
    summary (a crash/error) → FAIL CLOSED with a clear "scanner error, not a
    finding" message. The old gate mapped ANY nonzero exit to (a), so a crash
    (exit 1, traceback, no summary) became a false DENY carrying the LYING
    "banned trailer found" message. Unlike the sibling coverage gate (which
    fails OPEN on a crash, correct for a coverage gate), a leak-prevention
    gate must NOT fail open — an unscanned publish may carry a signature.
    """
    src_dir = Path(__file__).resolve().parents[2] / "src"
    added = False
    try:
        if str(src_dir) not in sys.path:
            sys.path.insert(0, str(src_dir))
            added = True
        return _run_block_ai_signature(data)
    except Exception:  # noqa: BLE001 — a crashing gate is worse than no scan; fail open.
        return False
    finally:
        if added:
            with contextlib.suppress(ValueError):
                sys.path.remove(str(src_dir))


def _run_block_ai_signature(data: dict) -> bool:
    """Block-ai-signature inner body — assumes ``teatree`` is already importable."""
    payload = _extract_ai_sig_payload(data)
    argv = _ai_sig_scan_argv()
    if payload is None or argv is None:
        return False

    allowance = validator_timeout_seconds()
    try:
        result = subprocess.run(  # noqa: S603 — trusted internal subprocess; fixed argv, no shell
            argv,
            input=payload,
            capture_output=True,
            text=True,
            check=False,
            timeout=allowance,
        )
    except subprocess.TimeoutExpired:
        warn_validator_timed_out("AI-signature", allowance)
        return False
    except FileNotFoundError:
        return False

    finding = _ai_sig_finding(result.stdout or "")
    if finding is not None:
        return emit_pretooluse_deny(
            "BLOCKED: AI-signature / banned trailer in the PR body or commit message. "
            "Remove it before creating the PR/commit (BLUEPRINT §17.6 gate 15).\n" + finding
        )
    # A clean exit is ALLOW. A nonzero exit whose stderr shows a bootstrap-crash
    # marker means the scanner subprocess never RAN — a Django AppRegistryNotReady /
    # ImproperlyConfigured / ModuleNotFoundError import traceback, e.g. a `t3`
    # binary whose CLI eagerly loads Django before setup in a hook env with no
    # DJANGO_SETTINGS_MODULE. That is the "broken environment" case the docstring
    # promises to FAIL OPEN on (no t3 / import error / timeout) — a gate that
    # cannot run AT ALL must not lock out every commit. It is distinct from the
    # fail-closed case below: a scanner that RAN and errored (usage error,
    # malformed input) with no bootstrap crash still fails closed.
    if result.returncode == 0 or any(m in (result.stderr or "") for m in _AI_SIG_BOOTSTRAP_CRASH_MARKERS):
        return False
    # Scanner ran but exited nonzero WITHOUT a well-formed finding summary —
    # a crash/error (traceback, usage error), not a finding. This is a
    # SECURITY gate (it prevents publishing AI signatures under the user's
    # identity), so the safe posture is FAIL CLOSED with a clear
    # "scanner error" message — block, but never report a finding that did
    # not happen, and never silently let an unscanned publish through.
    # (The sibling COVERAGE gate fails OPEN here, correctly for ITS
    # purpose; a leak-prevention gate must not.)
    return emit_pretooluse_deny(
        "BLOCKED: AI-signature scanner error — it exited nonzero without a clean result, so the "
        "PR body / commit message could NOT be confirmed signature-free. This is a scanner error, "
        "not a detected trailer. Fix the scanner / environment and retry (BLUEPRINT §17.6 gate 15).\n"
        + (result.stderr or result.stdout or "").strip()
    )


# ── PreToolUse: pre-publish quote-scanner gate (#1213) ──────────────


def _mcp_privacy_gate_enabled() -> bool:
    """Whether the Slack-MCP arm of the publish-privacy gates is enabled (default True).

    Canary off-switch for the newly-reachable Slack-MCP arm of the #1213
    quote-scanner and #1218 bare-reference gates (#171): until the Slack
    matcher was added to ``hooks.json`` these handlers never fired on a
    Slack MCP write, so this flag lets the operator disable that arm alone
    without a code edit if the now-live gate misfires. Fails OPEN to enabled
    on a missing/broken config (the arm is the same risk class as the
    already-live Bash arm of the same gate), an explicit ``false`` disables
    it. The Bash arm of both gates is unaffected by this flag. See
    :func:`_teatree_bool_setting` for the shared bare-boolean semantics.
    """
    return _teatree_bool_setting("mcp_privacy_gate_enabled", default=True)


def handle_quote_scanner_pretool(data: dict) -> bool:
    """Refuse a publish whose body carries a verbatim user-quote pattern.

    Promotes the prose-only "never quote user verbatim" rule
    (``feedback_redcard_never_quote_user_on_public_repos.md``) to a
    deterministic pre-publish gate. Surfaces covered include Bash calls
    that publish to GitHub/GitLab/Slack/git itself (``gh issue create``,
    ``glab mr update``, ``git commit -m``, ``curl … chat.postMessage``
    and siblings), the per-overlay t3 publish family (``review
    post-comment``, ``review post-draft-note``, ``notify send``,
    ``ticket create-issue``, ``t3 slack react``), and the Slack MCP
    ``send_message`` tools.

    HIGH match ⇒ refuse via ``permissionDecision: deny`` + a reason that
    names the matched patterns and points at the ``--quote-ok`` /
    ``QUOTE_OK=1`` override. MEDIUM-only ⇒ stderr warning, publish
    proceeds. Every decision (including overrides) lands in the
    quote-scanner JSONL ledger so cold review can audit what the gate
    saw.

    Fail-open on any internal error: a crashing hook is worse than no
    scan. The handler bootstraps ``sys.path`` to import ``teatree`` from
    the sibling ``src/`` directory (the hook script runs in the user's
    session shell with no guarantee that ``teatree`` is already
    importable, #1314) and swallows any exception, returning ``False``
    so the tool use proceeds unchanged.

    The Slack-MCP arm (newly reachable via the ``mcp__.*[Ss]lack.*``
    matcher, #171) is governed by the ``[teatree]
    mcp_privacy_gate_enabled`` canary off-switch; the Bash arm always runs.
    """
    if is_slack_mcp_tool(data.get("tool_name", "")) and not _mcp_privacy_gate_enabled():
        return False
    src_dir = Path(__file__).resolve().parents[2] / "src"
    added = False
    try:
        if str(src_dir) not in sys.path:
            sys.path.insert(0, str(src_dir))
            added = True
        return _run_quote_scanner_pretool(data)
    except Exception as exc:  # noqa: BLE001 — crash-proof hook: any failure degrades, never breaks the tool call
        # Fail OPEN on any internal error (a crashing hook is worse than no
        # scan), but NOT silently: an unscanned body on the PUBLIC-egress publish
        # path is exactly the leak this gate exists to catch, so name the failure
        # loudly on stderr (mirroring the banned-terms gate, #F7.9). A failed
        # stderr write must itself never break the tool call.
        with contextlib.suppress(OSError):
            sys.stderr.write(
                "[teatree] NOTE: pre-publish quote-scanner gate (#1213) failed open on an internal error "
                f"({type(exc).__name__}: {exc}); the publish body was NOT scanned for verbatim user quotes. "
                "This is a fail-open safeguard (a crashing hook is worse than no scan), NOT a clean scan — "
                "fix the underlying error, or verify the body by hand before it reaches a public surface.\n"
            )
        return False
    finally:
        if added:
            with contextlib.suppress(ValueError):
                sys.path.remove(str(src_dir))


def _run_quote_scanner_pretool(data: dict) -> bool:
    """Quote-scanner inner body — assumes ``teatree`` is already importable.

    Split out of :func:`handle_quote_scanner_pretool` so the outer
    wrapper owns the ``sys.path`` bootstrap + fail-open exception
    handler (#1314) without inflating its return count.
    """
    from typing import cast  # noqa: PLC0415 — deferred: off the fast hook's load path

    from teatree.hooks import quote_scanner  # noqa: PLC0415 — deferred: cold-hook import after sys.path setup

    tool_name = data.get("tool_name", "")
    raw_input = data.get("tool_input", {}) or {}
    if not isinstance(raw_input, dict):
        return False
    tool_input = cast("quote_scanner.ToolInput", raw_input)

    payload = quote_scanner.extract_publish_payload(tool_name, tool_input, _resolve_cwd_repo(data))
    if payload is None:
        return False

    override = quote_scanner.has_quote_ok_override(tool_name, tool_input)
    result = quote_scanner.scan_text(payload)

    if override:
        quote_scanner.log_decision(
            tool_name=tool_name,
            decision="allow-override",
            result=result,
            override=True,
        )
        return False

    if result.has_high:
        command = tool_input.get("command", "")
        verdict = _resolve_quote_verdict(command, _resolve_cwd_repo(data))
        block_message = _quote_scanner_high_block_message(quote_scanner, tool_name, result, verdict)
        return emit_pretooluse_deny(block_message) if block_message is not None else False

    if result.has_medium:
        sys.stderr.write(quote_scanner.format_warn_message(result) + "\n")
        quote_scanner.log_decision(
            tool_name=tool_name,
            decision="warn",
            result=result,
            override=False,
        )
        return False

    quote_scanner.log_decision(
        tool_name=tool_name,
        decision="allow",
        result=result,
        override=False,
    )
    return False


# ── PreToolUse: refuse self-DM via the user-token MCP tools (#1464) ──

_SELF_DM_MCP_WRITE_TOOLS: frozenset[str] = frozenset(
    {
        "slack_send_message",
        "slack_add_reaction",
        "slack_schedule_message",
        "slack_send_message_draft",
    }
)


def _self_dm_gate_enabled() -> bool:
    """Whether the self-DM gate is enabled (default True).

    Fails OPEN to enabled on a missing/broken config; an explicit ``false``
    is the one-line kill-switch. See :func:`_teatree_bool_setting` for the
    shared bare-boolean semantics.
    """
    return _teatree_bool_setting("self_dm_gate_enabled", default=True)


def _self_dm_destination_ids() -> _SelfDmDestinations:
    # DB-only: the overlay registry and the global ``slack_user_id`` resolve from the
    # DB-home ``ConfigSetting`` store, so the gate self-identifies the operator there.
    return _read_self_dm_destinations()


def handle_block_self_dm_via_mcp(data: dict) -> bool:
    """Refuse a claude.ai Slack MCP write to the operator's own bot↔user DM.

    The ``mcp__claude_ai_Slack__slack_*`` write tools publish under the USER's
    OAuth token, so a post/react to the operator's own self-IM renders as
    user-authored and the loop's scanners then react to the agent's own message.
    teatree's egress chokepoints (the slack_voice_classifier, the on-behalf
    egress class) never see an MCP tool call, so this PreToolUse deny is the only
    place the write can be stopped.

    DENY scope: the MCP write tools (``slack_send_message``,
    ``slack_add_reaction``, ``slack_schedule_message``,
    ``slack_send_message_draft``) whose destination resolves to a self-DM id.
    Mirroring the canonical ``SlackBotBackend._is_self_dm``, a self-DM id is
    either a configured ``[overlays.*].slack_dm_channel_id`` (``D…``) OR a
    configured ``slack_user_id`` / global ``[teatree] slack_user_id`` (``U…``,
    which Slack opens as the self-IM). The reason points the caller at the
    bot-token path (``t3 teatree notify send -``). Posts to any other channel
    (colleague surfaces, governed by the on-behalf gate) pass through untouched.

    Fail direction (user decision): FAIL-CLOSED. The hook cannot self-identify
    the author without the config (no MCP token or network in the hook
    subprocess, and the tool-schema text is not part of the hook input), so an
    unreachable config store DENIES with an error naming the fix. A
    genuinely-empty configuration (store readable, nothing declared) is a real
    state, not an error, so it allows silently. The ``self_dm_gate_enabled = false``
    setting is the sanctioned explicit escape hatch (never a silent one).
    """
    if not _self_dm_gate_enabled():
        return False
    tool_name = data.get("tool_name", "")
    if _slack_tool_suffix(tool_name) not in _SELF_DM_MCP_WRITE_TOOLS:
        return False
    tool_input = data.get("tool_input", {}) or {}
    if not isinstance(tool_input, dict):
        return False

    destinations = _self_dm_destination_ids()
    if not destinations.resolved:
        return emit_pretooluse_deny(
            "SELF-DM REFUSED (fail-closed): could not read the bot↔user DM destination ids "
            "from the config store (the DB is missing, locked, or unreadable), so this gate "
            "cannot confirm the Slack MCP write is not a self-DM under the USER's OAuth "
            "token. Declare the per-overlay slack_dm_channel_id / slack_user_id keys via "
            "`t3 <overlay> config_setting set`, or set self_dm_gate_enabled to false to "
            "disable this gate explicitly (`t3 <overlay> config_setting set "
            "self_dm_gate_enabled false`). To DM the user now, use the bot-token path: "
            "`t3 teatree notify send -` (reads the body from stdin)."
        )

    destination = _self_dm_destination(tool_input, destinations.ids)
    if not destination:
        return False

    return emit_pretooluse_deny(
        f"SELF-DM REFUSED: this claude.ai Slack MCP write targets the operator's own "
        f"bot↔user DM ({destination}) under the USER's OAuth token, so it renders "
        f"as user-authored and the loop's scanners will react to the agent's own message. "
        f"Use the bot-token path instead: `t3 teatree notify send -` (reads the body from "
        f"stdin). Posts to colleague channels are unaffected by this gate."
    )


# ── PreToolUse: pre-dispatch quote-scanner gate (#1401) ─────────────


def _dispatch_quote_scan_enabled() -> bool:
    """Whether the pre-dispatch quote scan is enabled (default True, #1564).

    Fails OPEN to enabled on a missing/broken config so the gate keeps its
    protective default; an explicit bare ``false`` is the one-line kill-switch
    (``t3 <overlay> config_setting set dispatch_quote_scan_enabled false``). An
    UNKNOWN (non-boolean) value warns loudly and keeps the default — the
    misconfiguration is surfaced, not silently swallowed. See
    :func:`_teatree_bool_setting_loud` for the fail-loud semantics.
    """
    return _teatree_bool_setting_loud("dispatch_quote_scan_enabled", default=True)


def handle_dispatch_prompt_quote_scanner(data: dict) -> bool:
    """Refuse an ``Agent``/``Task`` dispatch whose prompt carries verbatim user-voice/PII.

    Companion to the #1213 publish-boundary gate
    (:func:`handle_quote_scanner_pretool`). The publish gate fires too late
    to stop a leak that travels through dispatch: the orchestrator pastes a
    verbatim user quote into a sub-agent brief as "context", the sub-agent
    loads it into model context, and faithfully echoes it into a later
    published MR/issue/note — by which point the verbatim is already in
    play. This gate closes that boundary: it scans the dispatch prompt
    BEFORE the sub-agent is spawned.

    REUSES the existing ``quote_scanner.scan_text`` detector (no second
    matcher). Only a HIGH-confidence match denies — MEDIUM attribution
    shapes pass silently, because the fleet dispatches constantly and a
    false-deny on an ordinary brief is costlier here than a warn. The
    opt-out is an in-prompt ``[quote-ok: <reason>]`` token (reason
    mandatory), mirroring the ``[skill-load-ok: <reason>]`` convention —
    the publish-side ``--quote-ok`` flag / ``QUOTE_OK=1`` env have no
    analogue inside a prompt body.

    Fail-open on any internal error (a crashing gate is worse than no
    scan): the ``sys.path`` bootstrap + exception swallow mirror the #1314
    posture of the publish gate. Every decision lands in the shared
    quote-scanner ledger so cold review can audit what the gate saw.

    Disabled entirely (pass-through) when
    ``[teatree] dispatch_quote_scan_enabled = false`` — the one-line
    kill-switch (#1564); an unknown (non-boolean) value warns loudly and
    keeps the protective default (enabled).
    """
    if not _dispatch_quote_scan_enabled():
        return False
    src_dir = Path(__file__).resolve().parents[2] / "src"
    added = False
    try:
        if str(src_dir) not in sys.path:
            sys.path.insert(0, str(src_dir))
            added = True
        return _run_dispatch_quote_scanner(data)
    except Exception:  # noqa: BLE001 — crash-proof hook: any failure degrades silently, never breaks the tool call
        return False
    finally:
        if added:
            with contextlib.suppress(ValueError):
                sys.path.remove(str(src_dir))


def _run_dispatch_quote_scanner(data: dict) -> bool:
    """Dispatch quote-scanner inner body — assumes ``teatree`` is importable.

    Split out of :func:`handle_dispatch_prompt_quote_scanner` so the outer
    wrapper owns the ``sys.path`` bootstrap + fail-open handler without
    inflating its return count (mirrors the #1213 split).
    """
    from typing import cast  # noqa: PLC0415 — deferred: off the fast hook's load path

    from teatree.hooks import quote_scanner  # noqa: PLC0415 — deferred: cold-hook import after sys.path setup

    tool_name = data.get("tool_name", "")
    raw_input = data.get("tool_input", {}) or {}
    if not isinstance(raw_input, dict):
        return False
    tool_input = cast("quote_scanner.ToolInput", raw_input)

    payload = quote_scanner.extract_dispatch_payload(tool_name, tool_input)
    if payload is None:
        return False

    result = quote_scanner.scan_text(payload)

    if quote_scanner.dispatch_quote_ok_reason(payload):
        quote_scanner.log_decision(
            tool_name=f"{tool_name}:dispatch",
            decision="allow-override",
            result=result,
            override=True,
        )
        return False

    if result.has_high:
        quote_scanner.log_decision(
            tool_name=f"{tool_name}:dispatch",
            decision="deny",
            result=result,
            override=False,
        )
        return emit_pretooluse_deny(quote_scanner.format_dispatch_block_message(result))

    # MEDIUM-only or clean: allow silently (no stderr warning on dispatch —
    # the fleet dispatches constantly; only HIGH is actionable here).
    quote_scanner.log_decision(
        tool_name=f"{tool_name}:dispatch",
        decision="allow",
        result=result,
        override=False,
    )
    return False


# ── TaskCreated: quote-scanner gate (#171, task-list arm) ─────────


def _dispatch_quote_gate_on_task_create_enabled() -> bool:
    """Whether the task-list quote gate is enabled (default OFF, opt-in).

    The PreToolUse dispatch-quote gate (:func:`handle_dispatch_prompt_quote_scanner`)
    keys on ``Agent``/``Task`` and is the ONLY interception point a sub-agent
    dispatch has (#4216). The task-LIST tools are a different family: they bypass
    ``PreToolUse`` entirely, so a quote pasted into a task-list ENTRY is reachable
    only on ``TaskCreated`` — the concern this arm covers. It ships default-OFF
    because its live enforcement behaviour is unvalidated: an unvalidated gate
    stays inert (never wedges the loop) until the operator deliberately enables it
    with ``[teatree] dispatch_quote_gate_on_task_create_enabled = true``.

    Fails CLOSED to disabled (missing config → False, broken → False) and returns
    True only on an explicit ``true``. This deliberately DIFFERS from
    :func:`_mcp_privacy_gate_enabled` (which fails OPEN to enabled): the Slack-MCP
    arm is the same risk class as an already-live gate, whereas this one's
    enforcement semantics are not yet validated. See
    :func:`_teatree_bool_setting` for the shared bare-boolean semantics.
    """
    return _teatree_bool_setting("dispatch_quote_gate_on_task_create_enabled", default=False)


def handle_dispatch_prompt_quote_scanner_on_task_create(data: dict) -> bool:
    """Deny a new ``Task`` whose subject/description carries a HIGH verbatim quote.

    The task-list arm of :func:`handle_dispatch_prompt_quote_scanner`: the
    ``PreToolUse`` gate is skipped on the task-LIST tools (only ``TaskCreated``
    reaches them), so a verbatim user-voice/PII fragment pasted into a task as
    "context" would sit in the list and could later be echoed into a published
    output — defeating the #1213 publish gate. This handler scans the
    ``task_subject`` + ``task_description`` through the SAME
    ``quote_scanner.scan_text`` detector (HIGH-severity deny only, mirroring the
    PreToolUse handler) before the entry is created. It never sees a sub-agent
    dispatch: that event has one producer, the ``TaskCreate`` tool (#4216).

    NEVER-LOCKOUT:
    this does NOT route through ``_fail_open_or_deny`` / ``_is_self_rescue``
    (those are PreToolUse/Bash-command-shaped; a ``TaskCreated`` event carries no
    command). The gate ships default-OFF (opt-in via ``[teatree]
    dispatch_quote_gate_on_task_create_enabled = true``) — a gate whose live
    enforcement behaviour is unvalidated stays inert by default. When enabled,
    the off-ramps that keep the operator from being locked out are: the opt-in
    flag itself (unset/``false`` to disable), the ``[quote-ok: <reason>]`` token
    in the subject/description (reuses :func:`quote_scanner.dispatch_quote_ok_reason`),
    a missing ``session_id`` (fail-open), an unreadable config store
    (fail-disabled), and ``main``'s per-handler exception swallow. The master
    ``danger_gate_fail_open`` switch still protects the operator because rescue
    commands run as ``Bash``, never as task-list entries.
    """
    session_id = data.get("session_id", "")
    if not session_id or not _dispatch_quote_gate_on_task_create_enabled():
        return False

    src_dir = Path(__file__).resolve().parents[2] / "src"
    added = False
    try:
        if str(src_dir) not in sys.path:
            sys.path.insert(0, str(src_dir))
            added = True
        return _run_dispatch_quote_scanner_on_task_create(data)
    except Exception:  # noqa: BLE001 — crash-proof hook: any failure degrades silently, never breaks the tool call
        return False
    finally:
        if added:
            with contextlib.suppress(ValueError):
                sys.path.remove(str(src_dir))


def _run_dispatch_quote_scanner_on_task_create(data: dict) -> bool:
    """Task-list quote-scan inner body — assumes ``teatree`` is importable.

    Split out of :func:`handle_dispatch_prompt_quote_scanner_on_task_create` so
    the outer wrapper owns the ``sys.path`` bootstrap + fail-open handler
    (mirrors the #1213/#1401 split). A HIGH match emits the ``TaskCreated``
    teammate-stop deny envelope (NOT the PreToolUse ``hookSpecificOutput`` deny).
    """
    from teatree.hooks import quote_scanner  # noqa: PLC0415 — deferred: cold-hook import after sys.path setup

    subject = data.get("task_subject", "") or ""
    description = data.get("task_description", "") or ""
    payload = f"{subject}\n{description}"

    if quote_scanner.dispatch_quote_ok_reason(payload):
        quote_scanner.log_decision(
            tool_name="TaskCreated:quote",
            decision="allow-override",
            result=quote_scanner.scan_text(payload),
            override=True,
        )
        return False

    result = quote_scanner.scan_text(payload)
    if result.has_high:
        quote_scanner.log_decision(
            tool_name="TaskCreated:quote",
            decision="deny",
            result=result,
            override=False,
        )
        return emit_task_create_deny(quote_scanner.format_dispatch_block_message(result))

    quote_scanner.log_decision(
        tool_name="TaskCreated:quote",
        decision="allow",
        result=result,
        override=False,
    )
    return False


# ── PreToolUse: block-uncovered-diff (#937 §17.6 gate 12) ───────────
#
# Gate 12's detection (``teatree.utils.diff_coverage`` / ``t3 tool
# diff-coverage``) shipped correct in #862 but was wired into ZERO
# automatic enforcement points (absent from CI, pre-commit and this
# ``PreToolUse`` chain). §17.6.3 requires it to run as a pre-merge gate
# and "return the PR to draft automatically". This handler is that
# wiring — it mirrors the sibling Gate-15 (``handle_block_ai_signature``)
# shape exactly: intercept the merge-class mutations that move a PR
# toward review/merge and ``deny`` when ``t3 tool diff-coverage`` fails.
#
# Trigger surface (the moment a PR moves toward review/merge — the
# "return to draft automatically" reverse is ``gh pr ready --undo``):
#   - ``gh pr ready`` un-drafting a PR (NOT ``gh pr ready --undo``,
#     which IS the gate's remediation)
#   - a NON-draft ``gh pr create`` / ``glab mr create``
# A draft PR is not yet under review, so draft creation does not fire;
# ``git commit`` is deliberately NOT a trigger — Gate 12 is pre-MERGE,
# not pre-commit (the commit-stage gates are §17.1-numbering / sync).
#
# Fail-open contract (#122): DENY only on an actual, successfully-computed
# uncovered-diff finding. The gate shells ``t3 tool diff-coverage --json``
# and denies *only* when stdout parses as the report JSON with
# ``passes == false``. A subprocess CRASH (the #122 lockout:
# ``diff-coverage`` imports the DEV-only ``coverage`` module, absent from
# the installed ``t3`` tool env, so a real run dies with
# ``ModuleNotFoundError`` → exit 1, traceback on stderr, no parseable
# stdout), a timeout, a missing ``t3``, or any nonzero exit without
# parseable report JSON FAILS OPEN — a broken environment must never deny
# a merge-class mutation. Treating a crash as a coverage finding turned
# every ``gh pr create`` into a deny; that is the bug this closes.


def _is_merge_class_mutation(data: dict) -> bool:
    """Whether this tool call moves a PR toward review/merge.

    ``gh pr ready`` (un-drafting) or a non-draft ``gh pr create`` /
    ``glab mr create`` or a ``gh api``/``glab api`` POST to a PR/MR
    collection endpoint (F2 — same semantic effect, same gate coverage
    needed). ``gh pr ready --undo`` (return-to-draft, the gate's own
    remediation) and ``--draft`` creation are excluded. The verb detection
    (:func:`coverage_gate.is_merge_class_command`) runs on the quote/heredoc-
    stripped skeleton, so a mere MENTION inside a quoted argument or heredoc
    body never fires the gate.
    """
    if data.get("tool_name") != "Bash":
        return False
    return _is_merge_class_command(data.get("tool_input", {}).get("command", ""))


def handle_block_uncovered_diff(data: dict) -> bool:
    """Refuse a PR un-draft / non-draft create whose diff fails Gate 12.

    Deterministic pre-merge enforcement of the per-diff coverage +
    mutation/revert gate (BLUEPRINT §17.6 gate 12, #937). The detection
    shipped correct in #862 but ran in zero automatic enforcement points
    — a vacuity gate that never fires is itself a false-completion
    surface. This makes it a code gate at the same pre-merge layer as
    the sibling Gate-15 AI-signature scan, reusing ``t3 tool
    diff-coverage --json`` as-is.

    Fail-open contract (#122): DENY only on an actual, successfully-
    computed uncovered-diff finding — a report JSON with ``passes`` false.
    A subprocess crash (``ModuleNotFoundError: No module named
    'coverage'`` when the dev-only dep is absent from the installed ``t3``
    env), a timeout, a missing ``t3``, or any nonzero exit without
    parseable report JSON FAILS OPEN. A broken environment must never deny
    a merge-class mutation; hooks must be crash-proof.
    """
    if not _is_merge_class_mutation(data):
        return False
    finding = _coverage_finding_for_command(data.get("tool_input", {}).get("command", ""), data.get("cwd"))
    if finding is None:
        return False
    return _fail_open_or_deny(
        data,
        "BLOCKED: per-diff coverage gate 12 failed (BLUEPRINT §17.6.3). An added production line is uncovered, or a "
        "changed symbol is not imported by a changed test — it reads name-level imports only, not `mod.sym()` "
        "attribute access. If the symbol is already exercised, add `from <module> import <symbol>` to a changed "
        "test to make the reference visible, then re-mark the PR ready (resolve the finding first).\n" + finding,
    )


# ── PreToolUse: orchestrator-execution-boundary (#836 §17.6 gate 2) ──
#
# The orchestrator (the MAIN agent) keeps the session responsive: it
# dispatches sub-agents and makes merge/clear decisions and should not
# tie its own session up running a LONG / HEAVY command (a test suite, a
# build, a dev server, a long sleep, a full-tree sweep) that belongs in a
# sub-agent (or, when run inline, behind ``run_in_background: true``).
# Quick orientation Bash — ``git status``, ``cat``, ``ls``, ``grep``, a
# ``git commit`` — is allowed; only the heavy/long-running shapes below
# are gated. This is the denylist inversion of the original allow-list
# (#115): 4.x-class agents need to inspect freely, so the gate now flags
# the narrow set of commands that actually hurt — never every Bash.
#
# The main-vs-sub-agent signal (#115 root cause: a sub-agent call carries a
# non-empty ``agent_id``, a main-agent call omits it) is ``_call_is_from_subagent``,
# imported aliased above from the shared ``orchestration_boundary_signals`` leaf.

# Pure-orchestration tools — always allowed for the main agent.
_ORCHESTRATION_TOOLS = {
    "Task",
    "TaskCreate",
    "TaskGet",
    "TaskList",
    "TaskUpdate",
    "Agent",
    "SendMessage",
    "AskUserQuestion",
}
# ``_PYTEST_VERB_RE`` / ``_PYTEST_VERB_FINDER`` (the VERB-position pytest matcher,
# anchored to a command head so a ``git commit -m '…pytest…'`` mention is not a
# false-deny — #1178) now live in the shared ``orchestration_boundary_signals``
# leaf, imported aliased above so this gate and the investigation nudge read one
# source. A TARGETED pytest run is cheap and must stay ALLOWED in the foreground
# main agent (#1825): only the whole suite ties the session up. The verb
# match above tells us a ``pytest`` invocation is present; this decides
# whether the args make it a single/targeted run. Targeted iff the
# segment after the verb carries a ``-k``/``--deselect <expr>``, a ``::``
# node-id, OR a specific ``*.py`` test file path. A bare ``pytest`` (no
# selector), ``pytest -q``, and a DIRECTORY arg (``pytest tests/``) are
# whole-suite and stay DENIED.
_PYTEST_TARGETED_RE = re.compile(
    r"(?:^|\s)(?:-k|--deselect)(?:[=\s]|$)"  # -k <expr> / --deselect <expr>
    r"|::"  # a node-id (path::Class::test)
    r"|(?:^|\s)\S*\.py(?:::|\s|$)"  # a specific .py file path
)
# HEAVY / long-running Bash shapes the main agent should not run inline.
# This is a HEURISTIC denylist (anchored, case-sensitive on the verb);
# the escape hatch is ``run_in_background: true`` (or, for a whole class
# of work, dispatching a sub-agent), plus a per-call ``[fg-ok: <reason>]``
# marker. When in doubt the command is ALLOWED — only an explicit match
# here, foreground, is gated. Patterns cover: Python/test runners, the
# interactive Django shells (``manage.py shell``/``shell_plus``/``dbshell``
# — the original 1h-hung RED-FLAG incident #1178), language/asset builds,
# dev servers, browser E2E (``playwright test``, ``nx run …:e2e`` AND bare
# ``nx e2e <target>``), container image AND compose builds (``docker
# build`` / ``docker compose build``), package installs/sync, long sleeps,
# full-tree recursive sweeps (the shapes that actually wedge a session).
# ``manage.py migrate`` is gated elsewhere (the ``_BLOCKED_COMMANDS``
# t3-CLI redirect); short ``t3 loop tick``/``ci``/``doctor`` are NOT slow
# and are deliberately not listed. Fast, BOUNDED ops are never matched
# (#3253): read-only git (``status``/``log``/``diff``/``show``/``fetch``),
# a bounded ``git push`` (a small-branch push is seconds — the pre-push
# hook only ever runs the fast early signal, never the full suite), and a
# bare ``--help``/``--version`` query of any verb. A TARGETED ``pytest``
# run is exempted in :func:`_deny_heavy_main_agent_bash` (the verb still
# matches here; the whole-suite-vs-targeted split is applied at deny time).
_ORCHESTRATOR_HEAVY_BASH_RE = re.compile(
    r"(?:" + _PYTEST_VERB_RE + r"|"
    r"\btox\b|"
    r"\bt3\s+\S+\s+(?:run|e2e|test)\b|"
    r"manage\.py\s+runserver|"
    r"manage\.py\s+(?:shell|shell_plus|dbshell)\b|"
    r"\bnx\s+(?:serve|run|e2e)\b|"
    r"docker\s+compose\s+(?:up|build)|"
    r"\bdocker\s+build\b|"
    r"(?:npx\s+)?playwright\s+test|"
    r"\bnpm\s+(?:run|install|ci)\b|"
    r"\b(?:pipenv|pip)\s+install\b|"
    r"\buv\s+sync\b|"
    r"vite\s+build|"
    r"\bwebpack\b|"
    r"\bcargo\s+(?:build|test)\b|"
    r"\bmake\b|"
    r"\bsleep\s+\d{2,}|"
    r"\bfind\s+\S+.*-exec\b|"
    r"\bls\s+-[a-zA-Z]*R[a-zA-Z]*\b"
    r")",
)

# ``[fg-ok: <non-empty-reason>]`` anywhere in the command is the per-call
# opt-out for the rare case the loop owner truly needs heavy output inline,
# mirroring the ``[skill-load-ok: <reason>]`` token. An empty reason does not
# unblock.
_FG_OK_RE = re.compile(r"\[fg-ok:\s*\S[^\]]*?\s*\]")

# A heavy verb invoked purely to print its ``--help``/``-h``/``--version`` (and
# exit immediately) is trivially fast and read-only — NOT the long-running shape
# this gate guards (``t3 dream run --help``, ``docker build --help``,
# ``pytest -h``). The lookahead requires the flag to be a complete token so a
# recursive ``ls -lhR`` (a flag bundle, not a help query) is not mistaken for one.
_HELP_OR_VERSION_RE = re.compile(r"(?:^|\s)(?:--help|-h|--version)(?=\s|$)")

# Heavy shapes a help token must NEVER exempt, because the token does not belong
# to the heavy verb's own fast --help path:
#   * ``find … -exec <cmd> …`` — a ``-h`` after ``-exec`` is the EXEC'd command's
#     flag (``grep -h`` = suppress filename, ``rm -h`` / ``chmod -h``, ``du -h`` =
#     human-readable), NOT a help query; the find-sweep itself is still heavy.
#   * recursive ``ls …R…`` — its ``-h`` is the human-readable flag bundle.
# Reuses the exact heavy sub-patterns so the two regexes can never drift.
_NEVER_HELP_EXEMPT_RE = re.compile(r"\bfind\s+\S+.*-exec\b|\bls\s+-[a-zA-Z]*R[a-zA-Z]*\b")


def _is_orchestration_action(data: dict) -> bool:
    """True when the tool call is a sanctioned orchestration verb.

    Only the non-Bash orchestration surfaces are judged here. Bash is
    decided by the heavy-command denylist in
    :func:`handle_enforce_orchestrator_boundary` (it needs the
    ``run_in_background`` flag the denylist consults).
    """
    tool_name = data.get("tool_name", "")
    if tool_name in _ORCHESTRATION_TOOLS:
        return True
    # MCP orchestration surfaces: Slack/messaging sends, GitHub/GitLab
    # *view*-class MCP reads. A conservative allow-list keeps the gate
    # from flagging the orchestrator's own coordination calls.
    return tool_name.startswith("mcp__") and (
        "send_message" in tool_name or tool_name.endswith(("_view", "_get", "_list", "_read")) or "_view_" in tool_name
    )


def _orchestrator_bash_gate_enabled() -> bool:
    """Whether the heavy-Bash boundary gate is enabled (default True).

    Fails OPEN to enabled on a missing/broken config so the gate keeps its
    protective default; an explicit ``false`` is the kill-switch that lets the
    user disable it with one config line (never a code edit). See
    :func:`_teatree_bool_setting` for the shared bare-boolean semantics.
    """
    return _teatree_bool_setting("orchestrator_bash_gate_enabled", default=True)


def _orchestrator_boundary_agent_gate_enabled() -> bool:
    """Whether the foreground-Agent-dispatch deny is enabled (default ON, #1733).

    The ``Agent`` arm of the orchestrator-boundary gate (#1442) is now LIVE: an
    ``Agent`` PreToolUse matcher is wired in ``hooks.json`` (#1646) so an Agent
    dispatch reaches this deny. Only an EXPLICIT ``run_in_background: False`` is
    denied (current CC omits the field ⇒ absent = background). The gate flipped
    to default-ON (#1733) after the attended dry-run that #1646 asks for; that
    dry-run is the user's pre-INSTALL gate, not a blocker to the code (hooks run
    from the INSTALLED plugin, so a worktree change cannot lock out the live
    session — it only takes effect post-merge + ``t3 update``).

    Every never-lockout off-ramp stays intact even default-ON: a sub-agent
    context, ``run_in_background: true``, a per-call ``[fg-ok: <reason>]`` token,
    the ``[teatree] orchestrator_boundary_agent_gate_enabled = false``
    kill-switch, the deny-circuit-breaker, AND — via :func:`_fail_open_or_deny`
    (#1692) — the self-rescue allowlist and the master ``danger_gate_fail_open``
    switch.

    (The ``Agent`` matcher is the ONLY interception point a sub-agent dispatch
    has (#4216): the task-LIST tools are what bypass ``PreToolUse``, and
    ``TaskCreated`` is THEIR event, not a second dispatch seam — its single
    producer is the ``TaskCreate`` tool body.)

    Fails OPEN to enabled on a missing/broken config so the gate keeps its
    protective default; only an explicit bare ``false`` is the kill-switch. See
    :func:`_teatree_bool_setting` for the shared bare-boolean semantics.
    """
    return _teatree_bool_setting("orchestrator_boundary_agent_gate_enabled", default=True)


def _deny_foreground_agent_dispatch(data: dict) -> bool:
    """#1442: deny a main-agent foreground ``Agent`` dispatch.

    A foreground dispatch blocks the orchestrator for the entire
    sub-agent runtime (often 30+ min) — a recurring failure (memory rule
    ``feedback_always_run_in_background_for_sub_agent_dispatch``). Only
    the main agent is governed; a sub-agent dispatching its own ``Agent``
    may pick foreground.

    Default-ON behind :func:`_orchestrator_boundary_agent_gate_enabled` (#1733)
    now that an ``Agent`` PreToolUse matcher is wired (#1646). Only an EXPLICIT
    ``run_in_background: False`` denies (current CC dispatches async and OMITS
    the field, so an absent field is background, allowed). The off-ramps are:
    the kill-switch flag, a sub-agent context, absent-or-``True`` background,
    a per-call ``[fg-ok: <reason>]`` token. The deny routes through
    :func:`_fail_open_or_deny` (#1692) so the self-rescue allowlist and the
    master ``danger_gate_fail_open`` switch relax it — never a bare lockout.
    """
    if not _orchestrator_boundary_agent_gate_enabled():
        return False
    if _call_is_from_subagent(data) or data.get("tool_input", {}).get("run_in_background") is not False:
        return False
    prompt = data.get("tool_input", {}).get("prompt", "")
    if isinstance(prompt, str) and _FG_OK_RE.search(prompt[:512]):
        return False
    return _fail_open_or_deny(
        data,
        "[main-agent-orchestration-guard] Foreground Agent dispatch "
        "DENIED in main agent context.\n"
        "Dispatch in the background (omit run_in_background or pass true) so the "
        "main agent is not blocked; add an explicit `[fg-ok: <reason>]` marker "
        "to the prompt for a genuine foreground dispatch, or disable this "
        "gate by setting the DB-home `orchestrator_boundary_agent_gate_enabled` to "
        "false (`t3 <overlay> config_setting set orchestrator_boundary_agent_gate_enabled false`).\n"
        "Memory rule: "
        "feedback_always_run_in_background_for_sub_agent_dispatch "
        "(RED CARD recurrence).",
    )


def _pytest_command_is_targeted(command: str) -> bool:
    """True when EVERY ``pytest`` invocation in ``command`` is a targeted run (#1825).

    A targeted run carries a ``-k``/``--deselect <expr>``, a ``::``
    node-id, or a specific ``*.py`` test file path in the segment after
    the verb (see :data:`_PYTEST_TARGETED_RE`). A bare/whole-suite
    ``pytest`` or a directory arg (``pytest tests/``) is NOT targeted, so
    a command containing one is whole-suite and stays gated. Each pytest
    verb's argument span is bounded by the next shell separator so a
    selector belonging to a LATER chained pytest cannot vouch for an
    earlier whole-suite one.
    """
    matches = list(_PYTEST_VERB_FINDER.finditer(command))
    if not matches:
        return False
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(command)
        segment = command[start:end]
        boundary = re.search(r"[;&|\n(){}]", segment)
        if boundary is not None:
            segment = segment[: boundary.start()]
        if not _PYTEST_TARGETED_RE.search(segment):
            return False
    return True


def _command_matches_non_pytest_heavy(command: str) -> bool:
    """True when ``command`` matches a heavy pattern OTHER than the ``pytest`` verb.

    The targeted-pytest exemption (#1825) must only relax a command whose
    sole heavy match is a targeted ``pytest`` — a ``pytest -k foo && npm
    install`` still denies on the ``npm install`` arm. Stripping the
    pytest verb tokens to bare placeholders before re-matching leaves any
    other heavy arm intact.
    """
    stripped = _PYTEST_VERB_FINDER.sub(" __pytest__ ", command)
    return bool(_ORCHESTRATOR_HEAVY_BASH_RE.search(stripped))


def _heavy_command_is_help_only(command: str) -> bool:
    """True when EVERY heavy denylist match in ``command`` is a --help/--version query.

    A help/version invocation of a heavy verb prints usage and exits immediately
    (fast, read-only). Scoped per shell segment; a segment matching
    ``_NEVER_HELP_EXEMPT_RE`` (find-exec / recursive-ls / git-push) is never
    exempted even with a help token. False when no heavy segment present.
    """
    saw_heavy = False
    for segment in re.split(r"[;&|\n(){}]", command):
        if _ORCHESTRATOR_HEAVY_BASH_RE.search(segment):
            saw_heavy = True
            if _NEVER_HELP_EXEMPT_RE.search(segment) or not _HELP_OR_VERSION_RE.search(segment):
                return False
    return saw_heavy


def _deny_heavy_main_agent_bash(data: dict) -> bool:
    """Deny a main-agent foreground HEAVY/long-running ``Bash`` command.

    Passes through when the call is a sanctioned orchestration verb,
    comes from a sub-agent, is dispatched with ``run_in_background:
    true``, carries a ``[fg-ok: <reason>]`` opt-out marker, is a TARGETED
    ``pytest`` run with no other heavy arm (#1825), or does not match the
    heavy denylist (:data:`_ORCHESTRATOR_HEAVY_BASH_RE`).

    The deny routes through :func:`_fail_open_or_deny` (#1692) so the
    self-rescue allowlist and the master ``danger_gate_fail_open`` switch
    relax it like every other over-deny gate — a belt-and-braces on top of
    the self-rescue command never matching the heavy denylist.
    """
    if _is_orchestration_action(data) or _call_is_from_subagent(data):
        return False
    tool_input = data.get("tool_input", {})
    if tool_input.get("run_in_background") is True:
        return False
    command = tool_input.get("command")
    if not isinstance(command, str):
        return False
    if (
        _FG_OK_RE.search(command)
        or not _ORCHESTRATOR_HEAVY_BASH_RE.search(command)
        or _heavy_command_is_help_only(command)
    ):
        return False
    if _pytest_command_is_targeted(command) and not _command_matches_non_pytest_heavy(command):
        return False
    return _fail_open_or_deny(
        data,
        "BLOCKED: the orchestrator (main agent) ran a command that looks "
        "long-running / heavy and would tie up this session: "
        f"`{command[:120]}`.\n"
        "The orchestrator is delegate-only for heavy work (BLUEPRINT "
        "§17.4 / §17.8 / §17.6 gate 2). Either pass `run_in_background: "
        "true` to run it without blocking the session, dispatch a "
        "sub-agent (Task/Agent) to do it, add an explicit "
        "`[fg-ok: <reason>]` marker if you truly need the output inline, "
        "or — if this is a false positive — set the DB-home "
        "`orchestrator_bash_gate_enabled` to false "
        "(`t3 <overlay> config_setting set orchestrator_bash_gate_enabled false`) to disable the gate.",
    )


def handle_enforce_orchestrator_boundary(data: dict) -> bool:
    """Flag the MAIN agent running a HEAVY/long-running Bash command.

    Deterministic enforcement of the orchestrator-decides /
    loop-executes topology (BLUEPRINT §17.4 / §17.8 / §17.6 gate 2): the
    orchestrator keeps its session responsive by delegating long work.
    When the main agent (not a sub-agent — see
    :func:`_call_is_from_subagent`) runs a foreground Bash command that
    matches the heavy denylist (:data:`_ORCHESTRATOR_HEAVY_BASH_RE`) and
    is not dispatched with ``run_in_background: true`` (nor carrying a
    ``[fg-ok: <reason>]`` opt-out), the call is blocked with an actionable
    message. Everything else — quick orientation Bash, ``git``
    reads/commits, ``cat``/``ls``/``grep`` — passes; the ``pytest`` verb
    is anchored so a ``git commit -m '…pytest…'`` / ``uv add
    pytest-django`` is NOT a false-deny. Sub-agents are unaffected: they
    are the hands that implement and may run any command, heavy or not.
    The ``Agent`` foreground guard (#1442) rides the same handler.

    Disabled entirely (pass-through) when
    ``[teatree] orchestrator_bash_gate_enabled = false`` — the one-line
    kill-switch (#115).
    """
    if not _orchestrator_bash_gate_enabled():
        return False
    tool_name = data.get("tool_name", "")
    if tool_name == "Agent":
        return _deny_foreground_agent_dispatch(data)
    if tool_name != "Bash":
        return False
    return _deny_heavy_main_agent_bash(data)


# ── UserPromptSubmit + PreToolUse: orchestrator turn-budget nudge ────
#
# The orchestrator stays responsive only if its TURNS stay short — a turn
# that fires 20 tool calls before yielding makes the session feel dead to
# a user trying to interject. The heavy-Bash gate above governs long
# single OPERATIONS; this governs long TURNS. It is a SOFT advisory nudge,
# never a deny: once a main-agent turn crosses a responsiveness threshold,
# a one-time ``additionalContext`` line steers the orchestrator to wrap up
# and yield to the user. It can never lock the orchestrator out — it does
# not write a deny.
#
# TWO independent dimensions fire the SAME yield nudge (#1733 §2):
#   * COUNT (#1727)  — the turn made more than N non-orchestration tool calls;
#   * WALL-CLOCK     — more than T seconds of wall-clock elapsed since the
#                      turn started (the last user-visible action), regardless
#                      of how few tool calls were made. This catches the
#                      slow-but-few-calls failure the count dimension misses
#                      (a handful of long-blocking calls tying the session up).
# Either crossing nudges once per turn; both thresholds are config-driven and
# fail-open, and both re-arm every user turn.
#
# Only the main agent is governed (a sub-agent's turn is its whole job and
# must run to completion). Pure-orchestration tool calls — talking to the
# user, dispatching sub-agents, posting status — are FREE: they neither
# count toward the budget nor get nudged, because yielding to the user is
# itself an orchestration action.

_TURN_TOOL_COUNT_SUFFIX = "turn-tool-count"
_TURN_NUDGED_SUFFIX = "turn-budget-nudged"
_TURN_START_SUFFIX = "turn-start-monotonic"
_DEFAULT_ORCHESTRATOR_TURN_BUDGET = 25
_DEFAULT_ORCHESTRATOR_WALL_CLOCK_SECONDS = 180


def _orchestrator_turn_budget() -> int:
    """Soft per-turn tool-call budget for the main agent (default 25; 0 ⇒ off).

    DB-first read of ``[teatree] orchestrator_turn_budget`` via the shared
    :func:`_teatree_int_setting` adapter (config-unify PR4), TOML as never-lockout
    fallback. ``minimum=0`` keeps an explicit ``0`` valid — it disables the nudge
    with one config line, never a code edit — while a below-zero or non-int value
    falls back to the default.
    """
    return _teatree_int_setting("orchestrator_turn_budget", default=_DEFAULT_ORCHESTRATOR_TURN_BUDGET, minimum=0)


def _orchestrator_turn_wall_clock_threshold() -> int:
    """Wall-clock responsiveness threshold for the main agent (default 180s; 0 ⇒ off).

    DB-first read of ``[teatree] orchestrator_turn_wall_clock_seconds`` via the
    shared :func:`_teatree_int_setting` adapter (config-unify PR4), TOML as
    never-lockout fallback. ``minimum=0`` keeps an explicit ``0`` valid — it
    disables the wall-clock dimension with one config line — while a below-zero or
    non-int value falls back to the default.
    """
    return _teatree_int_setting(
        "orchestrator_turn_wall_clock_seconds", default=_DEFAULT_ORCHESTRATOR_WALL_CLOCK_SECONDS, minimum=0
    )


def handle_reset_turn_tool_budget(data: dict) -> None:
    """UserPromptSubmit: reset the per-turn responsiveness counters and nudge marker.

    A fresh user turn re-arms BOTH responsiveness dimensions — the orchestrator
    gets its full count budget and a fresh wall-clock window. Advisory only;
    never blocks the prompt.
    """
    if not isinstance(data, dict):
        return
    session_id = data.get("session_id", "")
    if not isinstance(session_id, str) or not session_id:
        return
    for suffix in (_TURN_TOOL_COUNT_SUFFIX, _TURN_NUDGED_SUFFIX, _TURN_START_SUFFIX):
        try:
            _state_file(session_id, suffix).unlink(missing_ok=True)
        except OSError:
            continue


_TURN_BUDGET_NUDGE_COUNT = (
    "[orchestrator-responsiveness] This turn has now made {count} tool calls (soft budget {budget})."
)
_TURN_BUDGET_NUDGE_WALL_CLOCK = (
    "[orchestrator-responsiveness] This turn has now run {elapsed}s of wall-clock (soft threshold {threshold}s)."
)
_TURN_BUDGET_NUDGE_TAIL = (
    " To keep the session responsive, wrap up the current step and YIELD to the "
    "user: dispatch any remaining heavy work to a background sub-agent (`Agent` "
    "with `run_in_background: true`), then end the turn so a new user message can "
    "be read. Orchestrate — don't keep grinding inline."
)


def _bump_turn_tool_count(session_id: str) -> int:
    """Increment and persist the per-turn tool-call counter; return the new count.

    Returns ``0`` (a no-op sentinel below the budget) if the state file can't be
    written — the nudge must never crash the hook.
    """
    count_file = _state_file(session_id, _TURN_TOOL_COUNT_SUFFIX)
    try:
        count = int(count_file.read_text(encoding="utf-8").strip()) if count_file.is_file() else 0
    except (OSError, ValueError):
        count = 0
    count += 1
    try:
        count_file.write_text(str(count), encoding="utf-8")
    except OSError:
        return 0
    return count


def _turn_elapsed_seconds(session_id: str) -> int:
    """Wall-clock seconds since this turn started (the last user-visible action).

    The turn-start monotonic timestamp is stamped lazily on the first tool call
    of a turn (and cleared every user prompt by
    :func:`handle_reset_turn_tool_budget`). Returns ``0`` when the start cannot
    be read/written — the wall-clock dimension then never fires this call rather
    than crashing the hook.
    """
    start_file = _state_file(session_id, _TURN_START_SUFFIX)
    now = time.monotonic()
    if start_file.is_file():
        try:
            return max(0, int(now - float(start_file.read_text(encoding="utf-8").strip())))
        except (OSError, ValueError):
            return 0
    with contextlib.suppress(OSError):
        start_file.write_text(repr(now), encoding="utf-8")
    return 0


def _emit_turn_budget_nudge_once(session_id: str, message: str) -> None:
    """Print the yield-to-user nudge at most once per turn (idempotent marker)."""
    nudged_marker = _state_file(session_id, _TURN_NUDGED_SUFFIX)
    if nudged_marker.exists():
        return
    try:
        nudged_marker.write_text("1", encoding="utf-8")
    except OSError:
        return
    print(json.dumps({"additionalContext": message + _TURN_BUDGET_NUDGE_TAIL}))  # noqa: T201 — hook writes its protocol output to stdout


def handle_orchestrator_turn_budget_nudge(data: dict) -> None:
    """PreToolUse: once per turn, nudge the main agent to yield to the user.

    TWO responsiveness dimensions fire the same yield nudge (#1733 §2).
    COUNT — NON-orchestration main-agent tool calls per turn (a fresh
    ``python3`` process each call, so the count persists in a per-session
    state file); the nudge fires once the count crosses
    :func:`_orchestrator_turn_budget`. WALL-CLOCK — seconds elapsed since the
    turn started (the last user-visible action); the nudge fires once the
    elapsed wall-clock crosses :func:`_orchestrator_turn_wall_clock_threshold`,
    independent of how few tool calls the turn made.

    Either crossing nudges at most once per turn (one idempotent marker shared
    by both dimensions). Sub-agents are exempt (their turn is their whole job);
    pure orchestration calls (:func:`_is_orchestration_action` — talking to the
    user, dispatching, status posts) are free and never trigger the nudge,
    because yielding is itself orchestration. Advisory only — never a deny, so
    it cannot lock the orchestrator out.
    """
    if not isinstance(data, dict):
        return
    if _call_is_from_subagent(data) or _is_orchestration_action(data):
        return
    session_id = data.get("session_id", "")
    if not isinstance(session_id, str) or not session_id:
        return
    budget = _orchestrator_turn_budget()
    wall_clock_threshold = _orchestrator_turn_wall_clock_threshold()
    if budget <= 0 and wall_clock_threshold <= 0:
        return
    _ensure_state_dir()
    elapsed = _turn_elapsed_seconds(session_id)
    count = _bump_turn_tool_count(session_id)
    if budget > 0 and count >= budget:
        _emit_turn_budget_nudge_once(session_id, _TURN_BUDGET_NUDGE_COUNT.format(count=count, budget=budget))
        return
    if wall_clock_threshold > 0 and elapsed >= wall_clock_threshold:
        _emit_turn_budget_nudge_once(
            session_id,
            _TURN_BUDGET_NUDGE_WALL_CLOCK.format(elapsed=elapsed, threshold=wall_clock_threshold),
        )


# ── PostToolUse: track-active-repo ──────────────────────────────────


def _extract_file_path(data: dict) -> str:
    tool_name = data.get("tool_name", "")
    tool_input = data.get("tool_input", {})

    if tool_name in _FILE_PATH_TOOLS:
        return tool_input.get("file_path", "")
    if tool_name in _PATH_TOOLS:
        return tool_input.get("path", "")
    if tool_name == "Bash":
        match = re.search(r"/(Users|home)/[^ \"]+", tool_input.get("command", ""))
        return match.group() if match else ""
    return ""


def _resolve_repo_key(file_path: str, workspace: str) -> str | None:
    if not file_path.startswith(f"{workspace}/"):
        return None

    relative = file_path[len(workspace) + 1 :]
    parts = relative.split("/")
    first = parts[0]
    main_repo_dir = Path(workspace) / first

    if (main_repo_dir / ".git").is_dir():
        return first

    if len(parts) < 2:  # noqa: PLR2004 — self-documenting literal in this context
        return None
    repo_in_wt = parts[1]
    wt_dir = main_repo_dir / repo_in_wt
    if not (wt_dir / ".git").exists():
        return None
    try:
        branch = subprocess.check_output(  # noqa: S603 — trusted internal subprocess; fixed argv, no shell
            ["git", "-C", str(wt_dir), "--no-optional-locks", "rev-parse", "--abbrev-ref", "HEAD"],  # noqa: S607 — trusted internal git invocation with a fixed argv
            text=True,
            timeout=3,
        ).strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return None
    return f"{branch}/{repo_in_wt}" if branch else None


def handle_track_active_repo(data: dict) -> None:
    """Track which repos the agent has touched during this session."""
    session_id = data.get("session_id", "")
    if not session_id:
        return

    file_path = _extract_file_path(data)
    if not file_path:
        return

    workspace = os.environ.get("T3_WORKSPACE_DIR", str(Path.home() / "workspace"))
    repo_key = _resolve_repo_key(file_path, workspace)
    if repo_key is None:
        return

    _ensure_state_dir()
    active = _state_file(session_id, "active")
    if repo_key not in set(_read_lines(active)):
        _append_line(active, repo_key)

    # MR cache invalidation
    if data.get("tool_name") == "Bash":
        command = data.get("tool_input", {}).get("command", "")
        if "git push" in command or "glab mr" in command:
            mr_cache = _state_file(session_id, "mr_refreshed")
            if mr_cache.is_file():
                mr_cache.unlink()


# ── PostToolUse + InstructionsLoaded: track-skill-usage ─────────────


def _skill_search_dirs() -> list[Path]:
    """Directories scanned to build the trigger index for closure resolution.

    ``T3_SKILL_SEARCH_DIRS`` (os.pathsep-separated) overrides the defaults —
    used by tests to point at a fixture skill tree. Otherwise: the plugin's
    own ``skills/`` directory plus the agent skill install locations.
    """
    override = os.environ.get("T3_SKILL_SEARCH_DIRS", "")
    if override:
        return [Path(d) for d in override.split(os.pathsep) if d]

    home = os.environ.get("HOME", "")
    candidates = [
        Path(__file__).resolve().parents[2] / "skills",
        Path(home) / ".agents" / "skills",
        Path(home) / ".claude" / "skills",
    ]
    return [d for d in candidates if d.is_dir()]


def _resolve_skill_closure(skills: list[str]) -> list[str]:
    """Expand *skills* to their ``requires:`` dependency closure.

    Uses the real trigger index (parsed from real SKILL.md frontmatter) and
    the real :func:`teatree.skill_support.deps.resolve_requires` resolver — a loaded
    skill's transitive dependencies are genuinely active and must be tracked.
    Unknown skills (framework skills with no trigger entry) pass through
    unchanged. On any resolution failure, fall back to the input skills so
    tracking never silently drops a genuinely-loaded skill.
    """
    if not skills:
        return []

    scripts_dir = Path(__file__).resolve().parents[2] / "scripts"
    src_dir = Path(__file__).resolve().parents[2] / "src"
    added: list[str] = []
    for extra in (str(scripts_dir), str(src_dir)):
        if extra not in sys.path:
            sys.path.insert(0, extra)
            added.append(extra)
    try:
        from lib.skill_loader import build_requires_index  # noqa: PLC0415 — deferred: cold-hook import

        from teatree.skill_support.deps import resolve_requires  # noqa: PLC0415 — deferred: cold-hook import

        index = build_requires_index(_skill_search_dirs())
        return resolve_requires(skills, index)
    except Exception:  # noqa: BLE001 — crash-proof hook: any failure degrades silently, never breaks the tool call
        return list(skills)
    finally:
        for extra in added:
            with contextlib.suppress(ValueError):
                sys.path.remove(extra)


def _record_skills(skills_file: Path, existing: set[str], closure: list[str]) -> None:
    """Append the already-resolved *closure* as canonical names, deduped.

    Each name is normalized UP to its fully-qualified form
    (:func:`normalize_skill_name`) before dedup so the persisted ``.skills``
    set stays canonical regardless of whether the source was the
    Skill-tool (already namespaced) or InstructionsLoaded (bare). The caller
    passes the pre-resolved closure (rather than re-resolving inside) so the
    recorded-set resolution happens exactly once per event.
    """
    for resolved in closure:
        name = normalize_skill_name(resolved)
        if name and name not in existing:
            existing.add(name)
            _append_line(skills_file, name)


def _maybe_engage_t3(session_id: str, names: list[str]) -> None:
    # #256 Option-1: a token that canonicalizes to the ``t3:`` namespace engages
    # the SUGGESTER via ``.t3-engaged`` — NOT ``.teatree-active`` (loops stay
    # reserved for teatree-requiring skills, preserving downstream-overlay loop
    # isolation). Canonicalize through the SAME identity seam
    # (:func:`normalize_skill_name`, normalize UP) that ``_is_teatree_skill``
    # uses, so a bare ``code`` and a qualified ``t3:code`` are detected
    # identically while a foreign ``other:review`` keeps its namespace and never
    # matches (no qualifier-stripping conflation).
    if any(normalize_skill_name(name).startswith("t3:") for name in names):
        _state_file(session_id, "t3-engaged").touch()


def handle_track_skill_usage(data: dict) -> None:
    """Track which skills are active this session, including their closure.

    A genuinely-loaded skill (Skill tool call or InstructionsLoaded entry)
    is expanded to its resolved ``requires:`` dependency closure before
    being recorded, so the statusline reflects the full active set — not
    just the explicitly tool-invoked name (#689). Suggested-but-not-loaded
    skills are never recorded here.
    """
    session_id = data.get("session_id", "")
    if not session_id:
        return

    _ensure_state_dir()
    skills_file = _state_file(session_id, "skills")
    existing = {normalize_skill_name(s) for s in _read_lines(skills_file)}

    # PostToolUse: single skill from tool_input
    skill_name = data.get("tool_input", {}).get("skill", "")
    if skill_name:
        _record_skills(skills_file, existing, _resolve_skill_closure([skill_name]))
        if _skill_load_activates_teatree([skill_name]):
            engage(session_id)
        _maybe_engage_t3(session_id, [skill_name])
        return

    # InstructionsLoaded: array of skill objects or skill name strings
    loaded: list[str] = []
    for skill_obj in data.get("skills", []):
        if isinstance(skill_obj, dict):
            name = skill_obj.get("name", "")
        elif isinstance(skill_obj, str):
            name = skill_obj
        else:
            continue
        if name:
            loaded.append(name)
    _record_skills(skills_file, existing, _resolve_skill_closure(loaded))
    if _skill_load_activates_teatree(loaded):
        engage(session_id)
    _maybe_engage_t3(session_id, loaded)


# ── PostToolUse: read-dedup ────────────────────────────────────────


def handle_read_dedup(data: dict) -> None:
    """Warn when a file is re-read without having changed since last read."""
    if data.get("tool_name") != "Read":
        return

    session_id = data.get("session_id", "")
    file_path = data.get("tool_input", {}).get("file_path", "")
    if not session_id or not file_path:
        return

    _ensure_state_dir()
    reads_file = _state_file(session_id, "reads")

    # Load existing reads: each line is "mtime\tpath"
    reads: dict[str, str] = {}
    for line in _read_lines(reads_file):
        parts = line.split("\t", 1)
        if len(parts) == 2:  # noqa: PLR2004 — self-documenting literal in this context
            reads[parts[1]] = parts[0]

    # Get current mtime
    try:
        current_mtime = str(Path(file_path).stat().st_mtime)
    except OSError:
        return

    prev_mtime = reads.get(file_path)
    if prev_mtime == current_mtime:
        print(  # noqa: T201 — hook writes its protocol output to stdout
            f"TOKEN SAVINGS HINT: {file_path} was already read this session "
            "and hasn't changed. Use your cached knowledge of its contents "
            "instead of re-reading."
        )

    # Update the reads file (overwrite to keep latest mtime per path)
    reads[file_path] = current_mtime
    reads_file.write_text(
        "\n".join(f"{mtime}\t{path}" for path, mtime in reads.items()) + "\n",
        encoding="utf-8",
    )


# ``handle_resolve_answered_question`` (+ its ``_answer_text_from_tool_response``
# helper) lives in the ``question_gates`` sibling — the AskUserQuestion
# decision-policy home — and is imported into the PostToolUse chain above.


# ``handle_track_agents`` (+ its ``_agent_id_from_response`` /
# ``_newest_task_agent_id`` helpers) lives in the ``dispatch_ledger`` sibling — the
# #778 sub-agent-dispatch capture — and is imported into the PostToolUse chain.


# ── PreCompact: retro-before-compact ──────────────────────────────


def _resolve_cwd_repo(data: dict) -> Path | None:
    """Resolve the harness-provided ``cwd`` to a directory, if any."""
    cwd = data.get("cwd", "")
    if not cwd:
        return None
    path = Path(cwd)
    return path if path.is_dir() else None


def _render_open_prs_section(repo: Path) -> list[str]:
    try:
        prs = _open_prs_for_repo(repo)
    except Exception:  # noqa: BLE001 — never block compaction on a lookup
        return []
    if not prs:
        return []
    lines = ["", "## Open PRs (this repo, @me, open)"]
    for pr in prs:
        number = pr.get("number", "?")
        title = pr.get("title", "(no title)")
        head = pr.get("headRefName", "")
        draft = " [draft]" if pr.get("isDraft") else ""
        suffix = f" — `{head}`" if head else ""
        lines.append(f"- #{number}{draft}: {title}{suffix}")
    return lines


def _render_no_commit_section(session_id: str) -> list[str]:
    """Surface sub-agents that terminated without committing (#1205).

    Reads the ``<session>.no-commit`` signals recorded by
    :func:`handle_subagent_stop_no_commit` so the post-compaction recovery
    snapshot tells the orchestrator NOT to assume the lost work landed.
    """
    no_commit = _read_lines(_state_file(session_id, "no-commit"))
    if not no_commit:
        return []
    lines = [
        "",
        "## Sub-agents that terminated WITHOUT committing (#1205)",
        (
            "These sub-agents ended on a work branch with 0 commits — their "
            "edits are lost on worktree teardown. Do NOT assume the work "
            "landed; re-dispatch each and require a commit before finishing."
        ),
    ]
    for line in no_commit:
        branch, _, worktree = line.partition("\t")
        lines.append(f"- branch `{branch}` at `{worktree}` — nothing committed")
    return lines


def _durable_session_snapshot(session_id: str, data: dict | None = None) -> str:
    """Build a recovery snapshot for *session_id* from DURABLE state only.

    Issue #778: a background sub-agent (a per-unit loop sub-agent,
    reviewer, task agent) auto-compacts without ever running
    ``/t3:retro``, so the behavioral "agent writes its own snapshot" path
    never fires for it. Reconstruct "who am I / what am I doing / where"
    purely from state that already outlives the transcript: whether this
    session is the loop-tick owner (#786 WS3 — a single Django-free
    ``_OWNER_LOOP`` record; there is no roster of singletons and no spawn
    brief) and the per-session active-repos / loaded-skills tracking
    files. No reliance on the agent having done anything.

    Issue #970: the original capture was too thin to actually resume —
    just the ever-touched ``.active`` ledger and the loaded skills. This
    additionally pins, when ``data`` carries the harness ``cwd``: the
    current worktree, branch, HEAD short SHA, uncommitted/unpushed
    counts, and the live open PRs for that repo (best-effort, never
    blocking). The live harness TODO list (via :func:`read_harness_todos`,
    #1734) rounds out "what was I about to do next" from the durable side.
    """
    data = data or {}
    lines = [
        f"# Auto-recovery snapshot — session `{session_id}`",
        "",
        (
            "Written by the PreCompact hook from durable state (no retro required). "
            "Use this to re-derive your identity and assignment after compaction."
        ),
    ]

    cwd_repo = _resolve_cwd_repo(data)
    if cwd_repo is not None:
        lines += ["", "## Current working directory", f"- `{cwd_repo}`"]
        lines += _render_git_state_section(cwd_repo)
        lines += _render_open_prs_section(cwd_repo)

    owned = [
        (name, entry)
        for name, entry in _read_loop_registry().items()
        if isinstance(entry, dict) and entry.get("session_id") == session_id
    ]
    if owned:
        lines += [
            "",
            "## Loop assignment",
            (
                "This session is the loop OWNER. The loops are tick-driven and PER-LOOP (#786 WS3, #2650): there is "
                "no master tick and no roster of long-lived sub-agents to resume — re-arm by ensuring each enabled "
                "loop's own native `/loop` (firing `t3 loops tick --loop <name>`) is registered for this session; "
                "each per-loop tick atomically claims the next pending unit via `t3 loop claim-next`."
            ),
        ]
        for _name, entry in sorted(owned):
            agent_id = entry.get("agent_id") or "(agent id not recorded)"
            lines.append(f"- tick-owner agentId `{agent_id}` (pid {entry.get('pid', '?')})")

    dispatched = _read_lines(_state_file(session_id, "agents"))
    if dispatched:
        lines += [
            "",
            "## Dispatched background sub-agents",
            (
                "Ad-hoc `Agent`-tool sub-agents dispatched this session "
                "(#778). Their agentIds are the handle `SendMessage` needs "
                "to resume / steer / collect a still-running agent — reuse "
                "them rather than re-dispatching duplicate work."
            ),
        ]
        for line in dispatched:
            agent_id, _, role = line.partition("\t")
            lines.append(f"- agentId `{agent_id}` — {role or '(no description)'}")

    lines += _render_no_commit_section(session_id)

    from teatree.core.harness_todos import read_harness_todos  # noqa: PLC0415 — lazy cold-import

    todos = read_harness_todos(session_id)
    if todos:
        lines += ["", "## Pending TODOs", *(f"- [{status}] {content}" for status, content in todos)]

    active = _read_lines(_state_file(session_id, "active"))
    if active:
        lines += ["", "## Repos touched this session", *(f"- {repo}" for repo in active)]

    skills = _read_lines(_state_file(session_id, "skills"))
    if skills:
        lines += ["", "## Skills loaded this session", f"- {', '.join(skills)}"]

    return "\n".join(lines) + "\n"


def _write_precompact_snapshot(session_id: str, data: dict | None = None) -> None:
    """Persist the durable-state snapshot under the recovery-recognized name.

    Reuses the ``t3-snapshot-`` prefix that :func:`_find_temp_files`
    (called by the SessionStart/compact recovery path, #845) already
    scans, keyed by session id with a fixed ``-precompact``
    suffix so a single deterministic file is overwritten each compaction
    (not an ever-growing pile). Best-effort: a snapshot write must never
    block compaction.
    """
    if not session_id:
        return
    target = STATE_DIR / f"{_T3_TEMP_PREFIX}{session_id}-precompact.md"
    # ``_ensure_state_dir`` (a ``mkdir``) is the more likely OSError source
    # than the write itself (read-only fs / parent perms) — both must be
    # suppressed so the docstring's "must never block compaction" holds.
    with contextlib.suppress(OSError):
        _ensure_state_dir()
        target.write_text(_durable_session_snapshot(session_id, data), encoding="utf-8")


def handle_pre_compact(data: dict) -> None:
    """Snapshot durable state, then nudge retro if lifecycle skills are active.

    The snapshot is unconditional and behavior-independent (issue #778):
    background sub-agents have no lifecycle skill loaded and would hit
    the retro-directive early return below, so the snapshot must be
    written BEFORE that return for them to recover post-compaction. The
    main-session retro directive is preserved unchanged after it.

    Note: *when* auto-compaction fires is governed by the Claude Code
    harness env var ``CLAUDE_AUTOCOMPACT_PCT_OVERRIDE`` (not a teatree
    setting); tune it at the harness-settings layer, not in teatree code.
    """
    session_id = data.get("session_id", "")
    if not session_id:
        return

    _write_precompact_snapshot(session_id, data)
    _run_prepare_stop_best_effort(session_id, data)

    skills_file = _state_file(session_id, "skills")
    loaded: set[str] = set()
    if skills_file.is_file():
        loaded = {line.strip() for line in skills_file.read_text(encoding="utf-8").splitlines() if line.strip()}

    lifecycle_skills = {"t3:code", "t3:debug", "t3:test", "t3:ship", "t3:review", "t3:ticket"}
    if not (loaded & lifecycle_skills):
        return

    json.dump(
        {
            "additionalContext": (
                "COMPACTION IMMINENT — lifecycle skills were active this session "
                f"({', '.join(sorted(loaded & lifecycle_skills))}). "
                "Run /t3:retro NOW to persist session learnings to memory before "
                "context is compressed. After retro completes, compaction will proceed."
            ),
        },
        sys.stdout,
    )


# ── Post-compaction snapshot recovery ─────────────────────────────
#
# Issue #845: the harness fires ``PostCompact``, but per the Claude Code
# hook response schema (``docs/claude-code-internals.md`` §3, sourced
# from ``claurst/spec/12_constants_types.md`` § 24.4) ``PostCompact``
# has NO ``hookSpecificOutput`` entry — a ``PostCompact`` hook cannot
# inject ``additionalContext`` and the harness discards its output. The
# only post-compaction event whose output the harness reads is
# ``SessionStart`` with ``source == "compact"``. Recovery therefore runs
# inside :func:`handle_session_start_bootstrap` (one stdout write,
# merged into the tick-dispatch directive). ``PreCompact`` still writes
# the durable snapshot with zero agent action — that side already works.


_T3_TEMP_PREFIX = "t3-snapshot-"
_TMP_DIR = Path(tempfile.gettempdir())


def _find_temp_files(session_id: str) -> list[Path]:
    """Find t3 temp files for this session in STATE_DIR and _TMP_DIR."""
    results: list[Path] = []
    session_glob = f"{_T3_TEMP_PREFIX}{session_id}-*.md"
    for search_dir in (STATE_DIR, _TMP_DIR):
        if search_dir.is_dir():
            results.extend(sorted(search_dir.glob(session_glob)))
    if _TMP_DIR.is_dir():
        for f in sorted(_TMP_DIR.glob(f"{_T3_TEMP_PREFIX}*.md")):
            if f not in results:
                results.append(f)
    return results


def _recover_snapshot_context(session_id: str) -> str | None:
    """Build the recovery directive from saved snapshots, or ``None``.

    Returns ``None`` when there is nothing to recover (no files, or only
    empty ones) so the caller can decide whether to emit anything.
    """
    files = _find_temp_files(session_id)
    if not files:
        return None

    parts: list[str] = []
    for f in files:
        try:
            content = f.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if content:
            parts.append(f"## {f.name}\n\n{content}")

    if not parts:
        return None

    return (
        "PRE-COMPACTION SNAPSHOTS RECOVERED — the following files were saved before "
        "context compaction. Read them to resume where you left off, then delete the "
        "temp files when done:\n\n" + "\n\n---\n\n".join(parts)
    )


# ── SessionStart: singleton loop orchestration bootstrap ────────────
#
# Issue #718. On every session start, emit an ``additionalContext``
# directive that idempotently establishes (or re-attaches to) the four
# machine-wide singleton loop sub-agents (the `t3-` loop roster). A
# second concurrent Claude session must NOT double-spawn the loops — it
# re-attaches to the recorded owner by agent id instead.
#
# The registry reuses the existing file + pid-liveness pattern (mirrors
# ``teatree.utils.singleton.read_pid``): a small JSON file in the teatree
# data dir, keyed by loop name, recording the live owner's session id +
# agent id + pid. It is deliberately NOT a DB row — this hook runs on
# every session start and the router is Django-free by design.
#
# Liveness subtlety: the hook router is a short-lived subprocess that
# exits the instant the hook returns, so ``os.getpid()`` would be dead
# before a second session ever starts (defeating the singleton). The
# owner-liveness pid must be the *Claude session* process — the hook's
# parent (``os.getppid()``) — which lives for the whole session. The
# SessionEnd hook additionally clears the entry on a clean exit, so the
# registry self-heals on both crash (pid dies) and graceful shutdown.

# #786 WS3: the immortal-roster name tuple (t3-main/review/cross-review/
# bug-hunt) is RETIRED — there is no fixed set of long-lived loop
# sub-agents. ``_OWNER_LOOP`` remains only as the single registry key
# identifying which *session* is the tick-owner (the Django-free anchor
# the #758/#810 Stop self-pump gates on).
_OWNER_LOOP = "t3-loop-tick-owner"

# Overridable for tests; the controlling terminal otherwise.
_TTY_PATH = "/dev/tty"


def _loop_registry_path() -> Path:
    """Return the machine-wide loop-registry JSON path.

    Sits alongside the existing ``*.pid`` flock files in the teatree
    data dir. ``T3_LOOP_REGISTRY_DIR`` overrides the directory (tests).
    Resolved without importing Django-heavy ``teatree.paths`` — the
    canonical default mirrors ``paths._TRUE_CANONICAL_DATA_DIR``.
    """
    override = os.environ.get("T3_LOOP_REGISTRY_DIR", "")
    base = (
        Path(override)
        if override
        else Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))) / "teatree"
    )
    return base / "loop-registry.json"


def _read_loop_registry() -> dict[str, dict]:
    path = _loop_registry_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _registry_lock_path() -> Path:
    """The flock file serializing every loop-registry write.

    A sibling of the registry JSON. Concurrent SessionStart/SessionEnd
    hooks across sessions race to claim/release ownership; without
    serialization a read-modify-write would lose updates and an
    interleaved ``tmp.replace`` could publish a torn file. The kernel
    ``flock`` releases on process death (crash-safe, no stale-pid
    window), mirroring ``teatree.utils.singleton``.
    """
    return _loop_registry_path().with_suffix(".lock")


@contextlib.contextmanager
def _registry_write_lock() -> Iterator[None]:
    """Hold an exclusive ``flock`` for the duration of a registry write.

    Stdlib-only (``fcntl``) so the Django-free hook router keeps no extra
    import cost on the common path. A blocking ``flock`` (not ``LOCK_NB``)
    because every writer must eventually win — the critical section is a
    sub-millisecond JSON dump.
    """
    import fcntl  # noqa: PLC0415 — deferred: off the fast hook's load path

    lock_path = _registry_lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)


def _write_loop_registry_locked(registry: dict[str, dict]) -> None:
    """Persist the registry assuming the registry flock is ALREADY held.

    The bare write body, callable from inside a ``_loop_registry_txn``
    critical section without re-acquiring the (non-reentrant, separate-fd)
    flock — a second blocking ``LOCK_EX`` on a fresh fd of the same file
    in this process would self-deadlock.
    """
    path = _loop_registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(registry, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _write_loop_registry(registry: dict[str, dict]) -> None:
    """Atomically (and flock-serialized) persist the loop registry.

    Serialized against concurrent cross-session writers via
    :func:`_registry_write_lock`; published via a ``tmp.replace`` rename
    so a reader never observes a partial file. Use :func:`_loop_registry_txn`
    instead when the decision depends on the current registry contents —
    a bare read-then-write is a TOCTOU across concurrent SessionStart
    hooks (two fresh sessions could both read "no owner" and both claim).
    """
    with _registry_write_lock():
        _write_loop_registry_locked(registry)


@contextlib.contextmanager
def _loop_registry_txn() -> Iterator[list[dict[str, dict]]]:
    """Atomic read-modify-write transaction over the loop registry.

    Holds the registry flock across the WHOLE critical section so a
    concurrent SessionStart/SessionEnd in another session cannot wedge
    between this transaction's read and write (the lost-update / double
    -claim TOCTOU). Yields a single-element list whose slot is the
    just-read registry; the caller mutates ``box[0]`` (or replaces it)
    and the committed value is written back under the same lock on a
    clean exit. On an exception nothing is written (the prior file
    stands).
    """
    with _registry_write_lock():
        box: list[dict[str, dict]] = [_read_loop_registry()]
        yield box
        _write_loop_registry_locked(box[0])


# ── #786 WS4: per-agent work-consolidation registry (invariant 3) ─────
#
# Invariant 3 of the #786 acceptance contract: exactly ONE per-agent
# work-consolidation loop (the issue's "todo-consolidation loop") per
# agent/sub-agent — per-actor, deduped by agent identity across ALL
# sessions (NOT per-session, NOT a global singleton). The consolidation
# loop IS the Stop self-pump. WS3 gated it
# on the single global tick-owner session (``_session_owns_loop``), which
# (a) collapsed it to one global loop and (b) keyed anti-spin by
# ``session_id`` so one agent spanning two sessions armed two markers.
#
# This registry is a SEPARATE JSON file from the tick-owner
# ``loop-registry.json`` (the tick-owner singleton — invariant 2 — and
# the per-agent consolidation loop — invariant 3 — are orthogonal
# concerns and must not share a keyspace). It reuses the WS3 substrate
# verbatim: the same ``_registry_write_lock`` flock, ``tmp.replace``
# publish, and ``_prune_dead_owner`` pid-liveness prune — no new locking
# or liveness primitive is invented. Keyed by ``agent_id``; each entry
# records the holding ``session_id``/``pid``/``heartbeat_ts``.


def _consolidation_registry_path() -> Path:
    """Per-agent consolidation registry JSON, beside ``loop-registry.json``.

    Same directory and ``T3_LOOP_REGISTRY_DIR`` override as the tick-owner
    registry (so test isolation redirects both at once) but a DISTINCT
    file — the tick-owner singleton and the per-agent consolidation loop
    are independent invariants and must not collide in one keyspace.
    """
    return _loop_registry_path().with_name("consolidation-registry.json")


def _read_consolidation_registry() -> dict[str, dict]:
    path = _consolidation_registry_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_consolidation_registry_locked(registry: dict[str, dict]) -> None:
    path = _consolidation_registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(registry, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _claim_agent_consolidation_slot(agent_id: str, session_id: str) -> bool:
    """Atomically claim the consolidation slot for ``agent_id``.

    Returns ``True`` iff this ``(agent_id, session_id)`` owns the single
    consolidation loop for that agent identity. ``False`` when a *live,
    different* session of the SAME agent already holds it (the
    cross-session dedup that makes the loop exactly-one-per-agent).

    The read → decide → write runs inside one ``_registry_write_lock``
    critical section (the WS3 flock, shared deliberately — both
    registries' writes are sub-millisecond and a single lock removes any
    lock-ordering hazard) so two concurrent ticks racing to claim the
    same agent cannot both win (the WS3 double-claim TOCTOU, applied
    per-agent). Dead-holder entries are pruned via ``_prune_dead_owner``
    (the existing pid-liveness primitive).

    #810 fail-safe: a ``Stop`` hook runs under whatever interpreter the
    harness invokes — ``teatree`` may be unimportable, so the
    pid-liveness primitive is unavailable. Without it a stale holder
    cannot be distinguished from a live one, so we CANNOT safely claim;
    return ``False`` (skip the pump) rather than claim on an unprunable
    registry and risk a duplicate consolidation loop. This matches the
    ``_session_owns_loop`` degradation contract (ownership unknown ⇒ do
    not pump) the pre-WS4 tests assert.
    """
    try:
        from teatree.utils.singleton import pid_alive  # noqa: F401, PLC0415 — deferred: cold-hook import; re-export
    except ImportError:
        return False
    with _registry_write_lock():
        registry = _prune_dead_owner(_read_consolidation_registry())
        holder = registry.get(agent_id)
        if holder is not None and holder.get("session_id") != session_id:
            return False
        registry[agent_id] = {
            "agent_id": agent_id,
            "session_id": session_id,
            "pid": os.getppid(),
            "heartbeat_ts": _now_ts(),
        }
        _write_consolidation_registry_locked(registry)
        return True


def _release_agent_consolidation_slot(session_id: str) -> None:
    """Drop every consolidation entry held by ``session_id`` (clean exit)."""
    with _registry_write_lock():
        registry = _read_consolidation_registry()
        survivors = {
            agent_id: entry
            for agent_id, entry in registry.items()
            if not (isinstance(entry, dict) and entry.get("session_id") == session_id)
        }
        if survivors != registry:
            _write_consolidation_registry_locked(survivors)


def _emit_osc_title() -> None:
    """Best-effort set the terminal tab title for the t3-master session.

    The interactive-TTY guard IS the openability of the controlling
    terminal: a non-interactive/headless session has no writable tty, so
    the ``open`` fails and the OSC is silently skipped. Never raised.
    """
    with contextlib.suppress(OSError), open(_TTY_PATH, "a", encoding="utf-8") as tty:  # noqa: PTH123 — builtin open on a device/proc path; Path.open adds nothing here
        tty.write("\033]0;TEATREE LOOP\007")


# #786 WS3: the per-loop spawn-brief machinery (_LOOP_SPAWN_BRIEFS /
# _loop_spawn_briefs / _brief_block / _DURABILITY_NOTE) is RETIRED — there
# is no immortal roster to re-spawn from a brief. The loop is the
# `t3 loops tick` cron + WS1 atomic claim-next + WS2 LoopLease; surviving
# an owner death is "the next session becomes tick-owner and keeps
# ticking", not "re-spawn N sub-agents from persisted briefs".


def _now_ts() -> int:
    return int(time.time())


_RENAME_REMINDER = (
    "\n\nReminder (UI-only, cannot be automated): run `/rename TEATREE LOOP` to name "
    "this owner session. The terminal tab title was set automatically where a TTY "
    "was available."
)


# ── #786 WS3: tick-dispatch directives (immortal roster retired) ──────
#
# The loop is no longer a fixed roster of long-lived sub-agents that a
# coordinator must keep alive / re-spawn on death/compaction. It is
# driven PER-LOOP (#2650): one native Claude ``/loop`` per enabled DB
# ``Loop`` row, each firing ``t3 loops tick --loop <name>`` on its own
# cadence — there is no master tick. Each per-loop tick atomically claims
# pending DB work (WS1 ``t3 loop claim-next`` — conditional-UPDATE CAS)
# and spawns a FRESH, BOUNDED sub-agent for just that unit, which returns.
# Statelessness across ticks IS the compaction-proofing — a worker dying
# mid-task leaves its Task reclaimable; the next tick re-dispatches it. The
# per-loop *executor* mutex is the WS2 ``LoopLease`` ``loop:<name>`` row;
# this Django-free hook registry only records which *session* owns a loop
# (one record, never a roster) so the #758/#810 Stop-hook self-pump can
# gate on it without a Django bootstrap in the hot path.

_TICK_DISPATCH_OWNER_DIRECTIVE = (
    "TEATREE LOOP — tick-driven per-loop, no roster to spawn.\n\n"
    "This session is a teatree loop OWNER. The loop is NOT a set of long-lived sub-agents you spawn or keep alive, "
    "and there is NO master tick: each enabled loop is its own native Claude `/loop` firing "
    "`t3 loops tick --loop <name>` on its own cadence. Each per-loop tick, claim the next pending unit atomically "
    "with `t3 loop claim-next` and spawn ONE fresh, bounded sub-agent for just that unit (it does the work and "
    "returns). No persistent loop roster, nothing to re-spawn on compaction — a worker dying mid-task leaves its "
    "Task reclaimable and the next tick re-dispatches it. Ensure each enabled loop's `/loop` is registered for "
    "this session." + _RENAME_REMINDER
)

_ACCOUNT_SWITCH_DIRECTIVE = (
    "TEATREE — Claude account switch detected (`/login`).\n\n"
    "The active Claude account changed since teatree last recovered the "
    "connectors, so the in-process MCP/backend token cache may still route "
    "Slack/Notion calls to the OLD workspace (delivery returns ok but the new "
    "account sees nothing — souliane/teatree#1176). Run `t3 doctor check` now: "
    "it invalidates the backend cache, re-probes each connector's live "
    "reachability, and records the new account so this notice clears. If a "
    "connector probes unreachable, re-auth that MCP connector in the Claude.ai "
    "UI before relying on any outbound message."
)

_MCP_CONNECTIVITY_DIRECTIVE = (
    "TEATREE — verify enabled MCP servers are connected.\n\n"
    "Enabled MCP servers are configured for this account. Run `t3 doctor check` "
    "now: it live-probes each enabled server (`claude mcp list`) and surfaces a "
    "LOUD, named finding for any enabled-but-disconnected server (or a provider "
    "mismatch). An enabled MCP that is not connected fails tool calls late and "
    "silently — confirm connectivity before relying on any MCP tool. If one is "
    "disconnected, reconnect it (re-auth the connector in the Claude.ai UI, or "
    "restart its local command) and re-run."
)

_TICK_DISPATCH_NON_OWNER_DIRECTIVE = (
    "TEATREE LOOP — tick-driven per-loop; another session owns the loop.\n\n"
    "Another live session owns the teatree loop(s) (owner session "
    "{owner_session}). Do NOT register competing per-loop `/loop`s and do "
    "NOT spawn loop sub-agents. The per-loop owner gate (#1073) is a HARD "
    "gate: a non-owner `t3 loops tick --loop <name>` will SKIP before any "
    "scanner / Slack DM-drain / dispatch runs at all — it does NOT execute the "
    "tick. Stay idle with respect to the loop. (If you ARE the user's main "
    "session and a foreign session has hijacked a loop, run `t3 loop "
    "claim --slot loop:<name> --take-over` and the hijacker's next tick SKIPs "
    "within one tick.)"
)


def _tick_owner_record(session_id: str, agent_id: str) -> dict[str, dict]:
    """Single owner-session record under ``_OWNER_LOOP`` (no roster, #786 WS3).

    The hook layer only needs *which session* is the tick-owner so the
    Stop-hook self-pump (#758/#810) can gate on it Django-free. The
    immortal-roster fields (per-loop ``spawn_brief``) are retired — there
    is nothing to re-spawn. The owner pid is ``os.getppid()`` (the
    long-lived session process, not this ephemeral hook subprocess), and it
    is recorded beside the namespace it resolves in (#4270) — the
    driver-detection probe attributes the integer by it before probing.
    """
    return {
        _OWNER_LOOP: {
            "session_id": session_id,
            "agent_id": agent_id,
            "pid": os.getppid(),
            "pid_namespace": _pid_namespace(),
            "heartbeat_ts": _now_ts(),
        }
    }


def _db_live_foreign_owner(session_id: str, current_pid: int | None) -> str:
    """Return the session id of a genuinely LIVE foreign ``t3-master`` DB lease, or ``""``.

    #1604: called when the file registry has no entry for the tick-owner (empty
    after prune / fail-safe) to detect registry/DB desync. Both the
    foreign-and-live decision and the #3968 exemption for the ``t3 worker`` that
    DRIVES the ticks belong to
    :func:`teatree.core.gates.t3_master_gate.live_foreign_owner_session`. This helper is
    only the disabled / bootstrap / fail-open envelope — any DB/import error
    returns ``""`` so a hiccup never blocks the SessionStart directive.
    """
    if _db_lease_consult_disabled():
        return ""
    if not bootstrap_teatree_django():
        return ""
    try:
        from teatree.core.gates.t3_master_gate import live_foreign_owner_session  # noqa: PLC0415 — needs Django

        return live_foreign_owner_session(session_id, current_pid=current_pid)
    except Exception:  # noqa: BLE001 — crash-proof hook: any failure degrades silently, never breaks the tool call
        return ""


def _evict_stale_db_lease_owner(session_id: str, current_pid: int | None) -> None:
    """Conditionally evict the ``LoopLease`` ``t3-master`` row (#1604).

    #1380 (#1107 follow-up). Context compaction rotates the Claude
    ``session_id``. The file registry's ``t3-loop-tick-owner`` slot is
    rewritten to the new id, but the DB ``LoopLease`` row name=
    ``t3-master`` still carries the OLD id with an unexpired
    ``lease_expires_at``. ``CLAUDE_SESSION_ID`` is empty in Bash-tool
    subprocesses (#1107) so the next ``t3 loops tick`` resolves the NEW
    id via the registry fallback and the ``claim_ownership`` CAS fails
    (DB row's session != new session, lease not expired) — the same
    session can never own its own loop until ``t3 loop claim
    --take-over`` runs manually.

    #1604 fix: the eviction now goes through
    ``LoopLease.objects.evict_stale_owner``, which consults the stored
    ``owner_pid`` and a liveness check before orphaning. A LIVE foreign
    lease (different live pid) is KEPT — only an expired, dead-pid, or
    same-process (post-compaction) lease is evicted. This closes the
    desync hijack: when the file registry is empty (e.g. pruned by the
    fail-safe) but the DB shows a live foreign lease, the new session
    stays idle instead of stealing the claim.

    Best-effort: any Django bootstrap / DB error fails open. The hook
    must never block the SessionStart directive over a DB hiccup.
    """
    if _db_lease_consult_disabled():
        return
    if not bootstrap_teatree_django():
        return
    try:
        from teatree.core.models import LoopLease  # noqa: PLC0415 — deferred: ORM import needs the app registry
    except Exception:  # noqa: BLE001 — crash-proof hook: any failure degrades silently, never breaks the tool call
        return
    try:
        LoopLease.objects.evict_stale_owner("t3-master", keep_session_id=session_id, current_pid=current_pid)
    except Exception:  # noqa: BLE001 — crash-proof hook: any failure degrades silently, never breaks the tool call
        return


def _autocompact_kill_switch_advisory() -> str | None:
    """Return the #980 advisory text when the harness kill-switch trips.

    The Claude Code harness silently disables auto-compaction on
    1M-capable models unless an
    explicit CLAUDE_CODE_AUTO_COMPACT_WINDOW (or settings.json
    autoCompactWindow) is set — CLAUDE_AUTOCOMPACT_PCT_OVERRIDE
    alone is silently dropped. The advisory tells the agent the
    matching env-var fix so it can patch ~/.claude/settings.json
    itself (see :mod:`teatree.core.autocompact_advisory` for the full
    decoded harness logic). Best-effort: any import / lookup failure
    returns None so the SessionStart directive always emits.
    """
    src_dir = Path(__file__).resolve().parents[2] / "src"
    added = False
    try:
        if str(src_dir) not in sys.path:
            sys.path.insert(0, str(src_dir))
            added = True
        from teatree.core.autocompact_advisory import AutocompactConfig, advisory_text  # noqa: PLC0415 — cold-hook read

        return advisory_text(AutocompactConfig.from_env())
    except Exception:  # noqa: BLE001 — crash-proof hook: any failure degrades silently, never breaks the tool call
        return None
    finally:
        if added:
            with contextlib.suppress(ValueError):
                sys.path.remove(str(src_dir))


def _account_switch_advisory() -> str | None:
    """Return the #1916 advisory when a `/login` account switch is pending.

    Uses the pure, Django-free fingerprint reader so the SessionStart hot path
    stays fast and crash-proof: compares the active ``oauthAccount.accountUuid``
    against the last-recovered one. Pure-read — does NOT record the new
    fingerprint or reset the cache (the network-bound recovery is `t3 doctor
    check`), so the directive keeps firing every session until recovery runs.
    Any import / read failure returns None so the directive never blocks.
    """
    src_dir = Path(__file__).resolve().parents[2] / "src"
    added = False
    try:
        if str(src_dir) not in sys.path:
            sys.path.insert(0, str(src_dir))
            added = True
        from teatree.core.account_fingerprint import fingerprint_switched  # noqa: PLC0415 — deferred: cold-hook import

        return _ACCOUNT_SWITCH_DIRECTIVE if fingerprint_switched() else None
    except Exception:  # noqa: BLE001 — never block SessionStart on a fingerprint read hiccup
        return None
    finally:
        if added:
            with contextlib.suppress(ValueError):
                sys.path.remove(str(src_dir))


def _mcp_connectivity_advisory() -> str | None:
    """Return the #2282 advisory when any MCP server is enabled.

    Uses the cheap, network-free ``~/.claude.json`` reader (NOT the live probe)
    to keep the network probe off the every-session SessionStart hot path: even
    within the 30s hook budget a slow or hung MCP endpoint would stall every
    session start, so session start nudges the agent to run ``t3 doctor check``
    (the bounded probe) only when there is something to verify. Any import /
    read failure returns None so the directive never blocks SessionStart.
    """
    src_dir = Path(__file__).resolve().parents[2] / "src"
    added = False
    try:
        if str(src_dir) not in sys.path:
            sys.path.insert(0, str(src_dir))
            added = True
        from teatree.core.mcp_connectivity import has_enabled_mcp_servers  # noqa: PLC0415 — deferred: cold-hook import

        return _MCP_CONNECTIVITY_DIRECTIVE if has_enabled_mcp_servers() else None
    except Exception:  # noqa: BLE001 — never block SessionStart on a config read hiccup
        return None
    finally:
        if added:
            with contextlib.suppress(ValueError):
                sys.path.remove(str(src_dir))


def _merge_session_start_context(context: str, session_id: str, source: str) -> str:
    """Prepend recovery snapshot + session hand-off, append the autocompact advisory.

    All merged into the ONE SessionStart stdout write — a second chained
    handler writing JSON would emit invalid concatenated JSON on stdout.

    #845: a ``source == "compact"`` resume reads back the PreCompact durable
    snapshot (the only post-compaction event whose ``additionalContext`` the
    harness honours). Session hand-off: a fresh / non-owner session claims an
    unclaimed hand-off (targeted at it, or parked for "next session") and
    injects the handing session's full durable state — ``claim_next`` excludes
    the session's own hand-off, so a same-session compact resume never
    re-injects its own snapshot. #980: surfaces the harness auto-compact kill-switch
    advisory when the env-var combo would silently disable auto-compaction.
    """
    if source == "compact":
        recovered = _recover_snapshot_context(session_id)
        if recovered is not None:
            context = f"{recovered}\n\n---\n\n{context}"

    handover = _claim_session_handover(session_id)
    if handover is not None:
        context = f"{handover}\n\n---\n\n{context}"

    autocompact = _autocompact_kill_switch_advisory()
    leading = (_account_switch_advisory(), _mcp_connectivity_advisory(), resume_admission_advisory(session_id, source))
    if autocompact:
        context = f"{context}\n\n---\n\n{autocompact}"
    for advisory in leading:
        if advisory:
            context = f"{advisory}\n\n---\n\n{context}"
    return context


def _emit_session_start_context(context: str) -> None:
    # #1452: the harness silently drops the legacy flat top-level
    # ``{"additionalContext": ...}`` form for SessionStart; the documented schema
    # (Agent SDK ``SessionStartHookSpecificOutput``) requires the nested envelope.
    # An empty merge (a not-engaged compact resume with no recovery context)
    # emits nothing (#256).
    if not context.strip():
        return
    json.dump(
        {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": context}},
        sys.stdout,
    )


def handle_session_start_bootstrap(data: dict) -> None:
    """Emit the tick-dispatch bootstrap directive (#786 WS3 — roster retired).

    The immortal-singleton roster (spawn/takeover/resume/re-attach a fixed
    set of long-lived loop sub-agents) is GONE. The loop is the
    ``t3 loops tick`` cron + WS1 atomic ``claim-next`` + WS2 ``LoopLease``
    tick mutex. This hook only decides which *session* is the tick-owner
    (one Django-free record, so the #758/#810 Stop self-pump can gate on
    it without a Django bootstrap) and orients the session accordingly:

    No live owner, or this session already owns it (e.g. post
    compaction): this session is/stays the tick-owner — claim it and emit
    the tick-dispatch owner directive. Post-compaction there is nothing
    to re-spawn (statelessness across ticks is the compaction-proofing);
    the same session simply continues ticking.

    A *different* live session owns it: stay idle w.r.t. the loop (a
    non-owner tick would find nothing to claim — #789 subsumed); never
    arm a competing tick or spawn loop sub-agents.

    The read → decide → write stays one flock-guarded transaction so two
    fresh sessions in the same window cannot both claim (TOCTOU).

    Gated on :func:`_loop_auto_load_active` (#256): a session only claims the
    tick-owner record / emits the bootstrap directive when it both opted into
    teatree AND the operator enabled session-start auto-load. Default OFF, so a
    colleague cloning the repo never silently becomes the loop owner.

    Default-off engagement (#256): when ``[teatree] autoload`` is set the owner
    default flips the session ``teatree-active`` BEFORE the loop gate, so today's
    bootstrap fires unchanged. When the session is neither autoloaded nor
    teatree-active, a FRESH start surfaces a one-line how-to-start advisory
    instead of returning silently; a compact/resume skips the advisory but still
    merges so snapshot-recovery / hand-off context is never dropped.
    """
    session_id = data.get("session_id", "")
    if not session_id:
        return
    source = data.get("source", "")
    skill_context = ""
    if _autoload_enabled():
        # #3869: resolve the context skills BEFORE ``engage(seed_skills=True)``. The seed
        # writes the lifecycle-core names into ``<session>.skills`` for the statusline, and
        # that file is the LOADED set the selection subtracts from — running after it would
        # let names that were never actually loaded suppress their own injection.
        skill_context = _session_start_skill_context(session_id)
        engage(session_id, seed_skills=True)
    elif not _teatree_active(session_id):
        advisory = "" if source in {"compact", "resume"} else _session_start_advisory()
        _emit_session_start_context(_merge_session_start_context(advisory, session_id, source))
        return
    if not _loop_auto_load_active(session_id):
        # The loop gate decides whether this session ARMS the loop machinery. It
        # must not also decide whether a parked hand-off is delivered (#3810):
        # the two are unrelated, and stapling the drain to the loop gate meant
        # any session that did not arm loops silently stranded the whole queue.
        # Every SessionStart path now merges, so exactly one thing gates the
        # drain — a session starting.
        _emit_session_start_context(_merge_session_start_context(skill_context, session_id, source))
        return
    agent_id = data.get("agent_id", "")

    became_owner_after_rotation = False
    current_pid = os.getppid()
    with _loop_registry_txn() as box:
        registry = _prune_dead_owner(box[0])
        owner = registry.get(_OWNER_LOOP)

        if owner is not None and owner.get("session_id") != session_id:
            # A different live session owns the tick — stay idle, never
            # arm a competing tick (#789 subsumed: a non-owner tick finds
            # nothing to claim anyway). Persist the prune only.
            box[0] = registry
            context = _TICK_DISPATCH_NON_OWNER_DIRECTIVE.format(
                owner_session=owner.get("session_id", "?"),
            )
            emit_osc = False
        elif owner is None:
            # No live registry owner (fresh machine OR dead-owner prune OR
            # #810 fail-safe returning {}). Before claiming, consult the DB
            # for a live foreign lease (#1604): the registry/DB can desync
            # when the incumbent's entry was pruned but its DB lease is
            # still valid. A live DB lease from a different session means
            # we are NOT the rightful owner — stay idle (INV1).
            db_live_owner = _db_live_foreign_owner(session_id, current_pid=current_pid)
            if db_live_owner:
                box[0] = registry
                context = _TICK_DISPATCH_NON_OWNER_DIRECTIVE.format(
                    owner_session=db_live_owner,
                )
                emit_osc = False
            else:
                # No live owner anywhere — this session is the tick-owner.
                # Mark for stale DB eviction (post-compaction path).
                became_owner_after_rotation = True
                box[0] = _tick_owner_record(session_id, agent_id or "")
                context = _TICK_DISPATCH_OWNER_DIRECTIVE
                emit_osc = True
        else:
            # This session already owns the registry — same-session restart
            # (post-compaction same-id, or hook re-fire). No eviction needed.
            box[0] = _tick_owner_record(session_id, owner.get("agent_id", "") if owner else agent_id or "")
            context = _TICK_DISPATCH_OWNER_DIRECTIVE
            emit_osc = True

    # #1380 / #1604: conditionally evict any stale DB ``t3-master`` row.
    # Runs when the registry had no entry (fresh machine or dead-owner prune)
    # and the DB also showed no live foreign lease, OR (#1838 PR#7a) on a
    # compaction resume — the eviction ORPHANS the stale lease (``session_id=""``)
    # synchronously before any tick, so the lead's next ``t3 loops tick``
    # re-anchors ``t3-master`` uncontested and no maker pane can win the
    # compaction-window CAS race against the rotated lead session. (The eviction
    # only orphans; it does NOT itself re-claim — the re-claim is the lead's next
    # tick.) The eviction is conditional on liveness either way
    # (``evict_stale_owner``'s decision table), so a LIVE foreign DB lease is
    # preserved — a pane never hijacks a genuinely live owner. ``current_pid``
    # is the lead's new process and ``keep_session_id`` its (rotated) session,
    # so a same-pid stale lease is recognised as a safe self-reclaim. Outside
    # the flock — the DB has its own CAS serialization; holding the registry
    # flock across a Django bootstrap would needlessly stall sibling
    # SessionStart hooks.
    if became_owner_after_rotation or source == "compact":
        _evict_stale_db_lease_owner(session_id, current_pid=current_pid)

    # OSC write is a tty side effect, not registry state — keep it out of
    # the flock critical section.
    if emit_osc:
        _emit_osc_title()

    # The skill directive leads: it is what the FIRST turn must act on, and the loop
    # bootstrap below it is orientation the agent does not act on immediately.
    context = _merge_session_start_context(
        "\n\n".join(part for part in (skill_context, context) if part), session_id, source
    )
    _emit_session_start_context(context)


def handle_session_end_loop_registry(data: dict) -> None:
    """Release the tick-owner record on a clean session exit (#786 WS3).

    The lifecycle counterpart to :func:`handle_session_start_bootstrap`:
    a clean exit relinquishes the single tick-owner record immediately,
    so the next session becomes tick-owner without waiting for
    pid-liveness to expire. Only the recorded owner's own SessionEnd
    clears it — a non-owner ending must not evict the live owner. (Post
    #786 WS3 there is one owner record, not a roster of slots.)
    """
    session_id = data.get("session_id", "")
    if not session_id:
        return
    with _loop_registry_txn() as box:
        registry = box[0]
        owner = registry.get(_OWNER_LOOP)
        if owner is not None and owner.get("session_id") == session_id:
            for name in [n for n, e in registry.items() if isinstance(e, dict) and e.get("session_id") == session_id]:
                del registry[name]
        # else: non-owner exit — leave the live owner untouched. box[0]
        # is the unchanged registry, so the txn rewrites it verbatim
        # (a harmless idempotent no-op write under the same lock).
        box[0] = registry


# ── Stop: per-session loop self-pump (#758 / board #50) ──────────────
#
# Replaces the manual coordinator pump. When the loop-OWNER session
# finishes a turn and consolidated work remains, the Stop hook returns
# ``{"decision": "block", "reason": ...}`` to self-continue the loop
# without an external re-prompt. No work => no block (idle by design,
# mirroring #748 "zero sessions = dead, accepted"). Non-owner sessions
# never pump (the loop-registry dedup from #718/#748 is authoritative).
# Anti-spin: a per-session ``<session>.pump-armed`` marker plus an
# mtime min-interval (same shape as ``_tick_meta_stale``) so a Stop
# storm cannot hot-loop. SessionEnd clears the marker.

_SELF_PUMP_MIN_INTERVAL = 60
_SELF_PUMP_PENDING_TIMEOUT = 5
_SELF_PUMP_PREVIEW = 5


def _consolidated_pending_work() -> list[dict]:
    """Return the loop's CLAIMABLE pending work via ``t3 loop pending-spawn``.

    ``--claimable-only`` (TODO #100) makes the probe budget-aware so a unit
    a full in-flight budget will refuse never re-arms the self-pump (the
    un-advanceable re-offer). ``[]`` on any failure so it fails safe to idle.
    """
    t3_bin = shutil.which("t3")
    if not t3_bin:
        return []
    try:
        result = subprocess.run(  # noqa: S603 — trusted internal subprocess; fixed argv, no shell
            [t3_bin, "loop", "pending-spawn", "--json", "--claimable-only"],
            capture_output=True,
            text=True,
            timeout=_SELF_PUMP_PENDING_TIMEOUT,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return []
    if result.returncode != 0 or not result.stdout.strip():
        return []
    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


def _session_owns_loop(session_id: str) -> bool:
    owner = _prune_dead_owner(_read_loop_registry()).get(_OWNER_LOOP)
    return owner is not None and owner.get("session_id") == session_id


def _session_drives_loop(session_id: str) -> bool:
    """True when this session is (or is the one expected to become) the loop driver.

    The single signal the loop-driven Stop gates (inline-question, completion-claim,
    standing-goal) share to decide "is this an autonomous/loop-driven turn vs an
    attended interactive one". Reuses the existing pid-anchored tick-owner
    registry (``_OWNER_LOOP`` / ``_session_owns_loop`` / ``_prune_dead_owner``)
    — no new ownership primitive. A session drives the loop when EITHER:

    - it already owns the live tick-owner record (the autonomous loop runner), OR
    - there is currently NO live owner anywhere (bootstrap / fresh machine /
        dead-owner prune). A no-owner session is the one expected to claim the
        loop at its next SessionStart, so it must still be treated as a driver —
        otherwise nobody is ever nagged to register and the loop never starts.

    It does NOT drive the loop only when a *different* live session owns the
    tick: that is the attended, non-owner interactive session the user is
    actually reading, so neither gate should fire there.

    DEGRADATION CONTRACT — FAIL SAFE (keep the gates firing): when ownership is
    unknown/unreadable the substrate already biases to "no owner" — a missing or
    corrupt registry makes ``_read_loop_registry`` return ``{}``, and an
    unimportable ``teatree`` makes ``_prune_dead_owner`` return ``{}``. Both land
    in the no-owner branch, so an unreadable signal yields ``True`` (driver) and
    both gates keep enforcing. An empty ``session_id`` is likewise treated as a
    driver here; the callers apply their own ``session_id`` exemptions.
    """
    if not session_id:
        return True
    owner = _prune_dead_owner(_read_loop_registry()).get(_OWNER_LOOP)
    if owner is None:
        return True
    return owner.get("session_id") == session_id


def _self_pump_recently_armed(marker: Path) -> bool:
    if not marker.is_file():
        return False
    return int(time.time()) - int(marker.stat().st_mtime) < _SELF_PUMP_MIN_INTERVAL


def _format_pending_summary(pending: list[dict]) -> str:
    preview = pending[:_SELF_PUMP_PREVIEW]
    lines = [
        f"  - task {p.get('task_id', '?')} → {p.get('subagent', '?')} "
        f"({p.get('phase', '?')}) {p.get('issue_url', '')}".rstrip()
        for p in preview
    ]
    if len(pending) > _SELF_PUMP_PREVIEW:
        lines.append(f"  - …and {len(pending) - _SELF_PUMP_PREVIEW} more")
    return "\n".join(lines)


def _actor_key(data: dict) -> str:
    """The identity the consolidation loop is deduped by (#786 invariant 3).

    The Stop payload's ``agent_id`` when present (the per-actor key —
    stable for one agent across sessions, distinct across agents);
    otherwise the ``session_id`` (a session with no separate agent
    identity is its own actor — the degenerate-but-correct case of "one
    loop per agent identity").
    """
    return data.get("agent_id") or data.get("session_id", "")


def handle_loop_self_pump(data: dict) -> bool | None:
    """Self-continue the per-agent consolidation loop on Stop (#786 WS4).

    Returns ``True`` (emitting a ``block`` decision) for the agent that
    owns the single consolidation slot for its identity (deduped across
    ALL sessions — invariant 3, NOT the global tick-owner singleton),
    with consolidated pending work, outside the anti-spin interval.
    Otherwise returns ``None`` (idle / deduped / spin-guarded) so the
    session may end normally.

    Crash-proof by contract (#810): a ``Stop`` hook must NEVER raise to
    the session. A broad boundary guard contains any unexpected error
    in the self-pump path (a missing/unimportable ``teatree``, registry
    I/O, etc.) to a single stderr line and a clean ``None`` — the
    session ends normally and the self-pump is simply skipped.
    """
    try:
        return _loop_self_pump(data)
    except Exception as exc:  # noqa: BLE001 — Stop hook must be crash-proof
        print(  # noqa: T201 — hook stderr is the module's logging channel
            f"[hook_router] loop self-pump skipped (unexpected error: {exc})",
            file=sys.stderr,
        )
        return None


_DISOWN_FALSEY: frozenset[str] = frozenset({"", "0", "false", "False"})


def _self_pump_suppressed(session_id: str) -> bool:
    """Is the Stop self-pump gated off for this session (#959)?

    The self-pump is a SINGLETON bound to the ONE designated t3-master
    session (the ``_OWNER_LOOP`` record — set at SessionStart, released
    at SessionEnd, transferable across sessions). WS4's "per-agent,
    decoupled from the tick-owner" model leaked the loop into EVERY
    fresh/unrelated session — a brand-new blog-writing session
    immediately started pumping ``t3 loops tick``/``claim-next`` and
    spawning review sub-agents. This gate is checked FIRST so a
    non-owner session's Stop hook is a clean no-op: no ``pending-spawn``
    subprocess, no registry write, no error noise in the transcript. The
    per-agent consolidation slot stays as a secondary cross-session
    dedup, NOT a substitute for this gate.

    A durable DB ``LoopState`` pause/disable of ``dispatch`` (the loop the
    self-pump drives) makes the owner's Stop hook a clean no-op so a paused
    control plane cannot busy-loop on stale pending work — the
    restart-surviving 'pause everything' (#1913,
    :func:`db_loop_state_suppresses_self_pump`). Loop control is ``/loops`` +
    the DB only; there is no env kill-switch.

    Immediate mitigation knob: ``T3_LOOP_DISOWN`` truthy (in the session's
    env or the bash env file, resolved via :func:`_resolve_loop_env`) makes
    even the owner's Stop hook a clean no-op, so a session can stop driving
    the loop in-process without touching the registry or ending the session.

    """
    if db_loop_state_suppresses_self_pump():
        return True
    if _resolve_loop_env("T3_LOOP_DISOWN").strip() not in _DISOWN_FALSEY:
        return True
    return not _session_owns_loop(session_id)


def _loop_self_pump(data: dict) -> bool | None:
    session_id = data.get("session_id", "")
    if not session_id:
        return None
    if _self_pump_suppressed(session_id):
        return None

    actor = _actor_key(data)

    _ensure_state_dir()
    # Anti-spin marker keyed by the ACTOR (agent identity), not the
    # session — one agent spanning two sessions must share one marker
    # (#786 WS4: pre-WS4 the session-keyed marker let the same agent
    # re-pump immediately in a fresh session).
    marker = _state_file(actor, "pump-armed")
    if _self_pump_recently_armed(marker):
        return None

    pending = _consolidated_pending_work()
    if not pending:
        return None

    # Exactly one consolidation loop per agent identity across all
    # sessions (invariant 3). A live different session of the SAME agent
    # already holding the slot ⇒ this one stays idle (deduped); the
    # claim is an atomic flock CAS so two concurrent ticks can't both win.
    if not _claim_agent_consolidation_slot(actor, session_id):
        return None

    marker.write_text("1", encoding="utf-8")
    # Tag the tick with the owner session id AND the durable session pid so
    # its re-claim heartbeat always lands under the real session and anchors
    # the lease on the long-lived session process — instead of resolving the
    # id to "" and the pid to os.getppid() of the torn-down Bash-tool shell
    # (#1107/#1722). The session id IS the owner here (the self-pump only
    # fires for the owner), and os.getppid() in this Stop hook IS that
    # durable session process (the same value SessionStart records in the
    # loop registry), so the pid-anchored claim keeps the lease anchored
    # even when the tick subprocess cannot read the registry (#1073).
    session_pid = os.getppid()
    reason = (
        "TEATREE LOOP SELF-PUMP — consolidated work remains; continue the loop "
        "without waiting for an external prompt. Repeatedly run "
        f"`T3_LOOP_SESSION_ID={session_id} T3_LOOP_SESSION_PID={session_pid} "
        "t3 loop claim-next` and spawn ONE fresh, bounded sub-agent (Agent tool) "
        "for each claimed unit until it returns nothing — the claim is atomic "
        "(#786 WS1), so no separate post-spawn claim step and no double-dispatch "
        "(the per-loop `/loop`s do the scanning; the self-pump only drains the "
        "already-pending work). Outstanding now:\n" + _format_pending_summary(pending)
    )
    json.dump({"decision": "block", "reason": reason}, sys.stdout)
    return True


def handle_session_end_self_pump(data: dict) -> None:
    """Release the per-agent consolidation slot + marker on session exit.

    Counterpart to the Stop self-pump (#786 WS4): a clean exit drops both
    the actor-keyed anti-spin marker and this session's consolidation
    registry entries, so a fresh session of the same agent can re-claim
    immediately instead of waiting for pid-liveness to expire.
    """
    session_id = data.get("session_id", "")
    if not session_id:
        return
    _state_file(_actor_key(data), "pump-armed").unlink(missing_ok=True)
    _release_agent_consolidation_slot(session_id)


# ── Stop: structured-question gate (#807) ───────────────────────────
#
# When the agent needs a decision it often asks INLINE in prose inside a
# long status/coordinator message. In an autonomous/loop setup the user
# may never read that — it reads like a log line, not a prompt — so the
# question is lost and work stalls or the agent guesses. Persisting "ask
# via the structured question tool" as a soft memory has NOT changed the
# behaviour (it recurs every session): a rule the agent must *remember*
# is not a control; only a hook is (the #730/#762/#804 durability theme).
#
# This Stop gate detects a user-directed question posed with NO
# AskUserQuestion tool call in the same (final) assistant turn and blocks
# — returning {"decision": "block", "reason": ...} so the agent must
# re-ask through the structured tool. There is intentionally NO `relax:`
# escape: it is a gate, like the other Stop-time gates above.
#
# Detection heuristic (tuned for precision over recall — a missed
# question is cheaper than a false block on a status turn):
#   1. The FINAL assistant turn (content since the last user message) has
#      a text block whose prose (fenced code blocks stripped, so a `?` in
#      a regex/glob does not count) contains a `?`, AND
#   2. that prose matches a second-person/decision cue ("want me to",
#      "should I", "shall I", "which", "do you", "would you like",
#      "… or …?", "prefer"), AND
#   3. no AskUserQuestion tool_use occurred anywhere in that final turn.
# A `?` alone (rhetorical aside, echoing the user, an explanatory
# sentence) does NOT trip the gate — the decision cue is required. A
# "soft ask" ("let me know if/whether …") is the one exception that trips
# without a `?`: it is the canonical lost-in-a-log-line failure mode. The
# `stop_hook_active` flag short-circuits so the gate cannot hot-loop on
# its own re-fire.

# The detection heuristic itself — ``is_user_directed_question`` and its
# ``?``/decision-cue/soft-ask regexes — lives in the ``question_gates`` sibling
# (imported above) alongside the one-decision-per-call warn; this handler keeps
# the routing decision (loop-ownership, transcript parsing, the block emit).


# ``_read_transcript_entries`` moved to the ``question_gates`` sibling (the
# transcript-parsing home) and imported back as ``_read_transcript_entries`` so
# this god-module stays under its LOC cap; callers below are unchanged.
def _entry_role(entry: dict) -> str | None:
    message = entry.get("message")
    if isinstance(message, dict):
        return message.get("role")
    return entry.get("type")


def _entry_content(entry: dict) -> list:
    message = entry.get("message")
    content = message.get("content", []) if isinstance(message, dict) else []
    return content if isinstance(content, list) else []


# ``_last_assistant_turn`` moved to the ``question_gates`` sibling (the
# transcript-parsing home, beside ``read_transcript_entries``) and imported back
# as ``_last_assistant_turn`` so this god-module keeps shrinking; callers and the
# ``completion_claim_gate`` re-export are unchanged.


# The block reason moved to the ``question_gates`` sibling (the detection home)
# so this shrink-only god-module nets smaller; imported above as
# ``STRUCTURED_QUESTION_BLOCK``.


_CLASSIFIER_RELAX_MARKERS = re.compile(
    # Protocol-specific vocabulary only.  Each alternative is a phrase the
    # sanctioned protocol actually produces and that does NOT appear in
    # ordinary Stop-gate prose:
    #   - "relax classifier" / "Allow it (relax classifier)": the shorthand
    #     label and verbatim option text.
    #   - "permissions.allow" ONLY when adjacent to a relax/classifier token
    #     (review NB1): a bare "permissions.allow" is unrelated allow-list
    #     prose and must still trip the #807 gate, so it is NOT a marker on
    #     its own — it must co-occur with "relax"/"classifier" within a short
    #     window.
    #   - "denied by the classifier/harness/auto mode": the denial-source
    #     phrasing.  A bare "was denied" is deliberately NOT a marker
    #     (review Finding 6) — "access was denied" / "the MR was denied" are
    #     ordinary prose and must still trip the gate.
    r"relax classifier"
    r"|Allow it \(relax classifier\)"
    r"|(?:relax|classifier)[^.]{0,80}?permissions\.allow"
    r"|permissions\.allow[^.]{0,80}?(?:relax|classifier)"
    r"|denied by (?:the )?(?:classifier|harness|auto[- ]?mode)",
    re.IGNORECASE,
)


def _is_classifier_relax_explanation(text: str) -> bool:
    """True when ``text`` looks like a Step-2 classifier-denial explanation.

    The sanctioned Classifier Denial Protocol (skills/rules/SKILL.md §
    "Classifier Denial Protocol") requires the agent at Step 2 to explain the
    denial in prose BEFORE calling AskUserQuestion at Step 3.  That prose
    contains classifier-specific markers that do not appear in ordinary
    decision questions, so we can distinguish them from questions that must
    still go through AskUserQuestion.

    Markers (any one is sufficient): "relax classifier" (the shorthand
    label), "Allow it (relax classifier)" (the exact option text),
    "permissions.allow" ONLY when it co-occurs with a relax/classifier
    token within a short window (review NB1 — a bare "permissions.allow" is
    unrelated allow-list prose and must still trip the gate), or "denied by
    the classifier/harness/auto mode" (the denial-source phrasing).  A bare
    "was denied" is intentionally NOT a marker (review Finding 6) so
    unrelated denials ("access was denied") still trip the #807 gate.

    This exemption is INTENTIONALLY NARROW.  It must not subsume arbitrary
    prose — it only applies to the narrow vocabulary of the denial protocol.
    """
    return bool(_CLASSIFIER_RELAX_MARKERS.search(FENCED_CODE_RE.sub(" ", text)))


def handle_enforce_structured_question(data: dict) -> bool | None:
    """Block a Stop whose final turn poses an inline user-directed question.

    Returns ``True`` (emitting a ``block`` decision) only when the last
    assistant turn contains a user-directed decision question (heuristic
    above) and no ``AskUserQuestion`` tool call occurred in that turn.
    Otherwise returns ``None`` so the session may end normally. The
    ``stop_hook_active`` re-fire flag short-circuits to avoid a hot loop.

    Exception — classifier-relax Step-2 turns: the sanctioned Classifier
    Denial Protocol requires the agent to explain the denial in prose (Step 2)
    BEFORE calling AskUserQuestion (Step 3).  That prose trips this gate
    because it contains decision cues but no tool call.  We detect it by
    ``_is_classifier_relax_explanation`` and let it through, avoiding the
    infinite block → explain → block loop.

    Context-aware: an inline question is invisible in an autonomous/loop run (it
    reads as a log line, so the decision is lost), but in an attended session a
    human IS reading the prose, so the gate is pointless nagging. Two attended
    signals skip it (mirroring ``handle_mirror_question_to_slack``): a LIVE USER
    TURN — the user typed a prompt seconds ago in THIS session
    (``_is_live_user_turn``), responding in real time, even when this session is
    the SessionStart-designated tick-owner (``_session_drives_loop`` true); and a
    NON-OWNER turn — a *different* live session owns the loop. It thus enforces
    only on a genuine autonomous turn: a driver verdict AND no live user turn.
    FAIL SAFE: unknown ownership or an ``_is_live_user_turn`` error keep it firing.
    """
    if data.get("stop_hook_active"):
        return None
    if _is_live_user_turn(data) or not _session_drives_loop(data.get("session_id", "")):
        return None
    turn = _last_assistant_turn(data.get("transcript_path", ""))
    if turn is None:
        return None
    text, used_question_tool = turn
    if used_question_tool or not is_user_directed_question(text):
        return None
    # Two never-block exemptions: a Step-2 classifier-relax explanation (the
    # sanctioned protocol explains the denial in prose before AskUserQuestion),
    # and a clarification right after the user rejected an AskUserQuestion (the
    # harness already routes that re-ask) — neither must be force-gated (#807).
    if _is_classifier_relax_explanation(text) or preceding_user_rejected_question_and_asked_clarify(
        _read_transcript_entries(data.get("transcript_path", ""))
    ):
        return None
    json.dump({"decision": "block", "reason": STRUCTURED_QUESTION_BLOCK}, sys.stdout)
    return True


# ── Classifier-relax settings.json allow gate ──────────────────────────────
# Moved WHOLE to the ``classifier_relax_gate`` sibling (the god-module is
# shrink-only), which also adds the #857 content-schema validation. The
# handler + detection primitives are re-exported at the top of this module.


# ── PostToolUse: track-cron-jobs ──────────────────────────────────────


# ── PreToolUse: block-out-of-band-merge (#126) ──────────────────────
#
# ``gh pr merge`` / ``glab mr merge`` bypass the FSM coherence mechanism
# (ledger update, MergeClear validation, SHA-binding, privacy/AI-signature
# scan, mark_merged), so a TEATREE-MANAGED repo must use the keystone
# transition ``t3 <overlay> ticket merge <clear_id>`` (BLUEPRINT §17.1
# invariant 8 / §17.4). But the previous static-regex block hard-denied
# EVERY repo — a lightweight repo with no ticket/overlay FSM had no merge
# path at all, a permanent lockout (#126).
#
# This cwd-aware gate carves out the unmanaged case: a merge is ALLOWED
# only when the cwd repo is confidently NOT teatree-managed (no overlay
# claims it). The gate stays STRICT for managed repos AND fail-safe on
# uncertainty: when the cwd or its slug cannot be resolved, the repo is
# treated as managed and the merge is BLOCKED — detection failure never
# weakens the gate.

_OUT_OF_BAND_MERGE_RE = re.compile(r"\b(?:gh\s+pr\s+merge|glab\s+mr\s+merge)\b")
# GitHub GraphQL merge-effecting mutations (all merge the PR / a branch out of band):
# mergePullRequest, enablePullRequestAutoMerge (native-rules merge), mergeBranch.
_GRAPHQL_MERGE_MUTATION_RE = re.compile(r"(?:mergePullRequest|enablePullRequestAutoMerge|mergeBranch)\s*\(")
_OUT_OF_BAND_MERGE_REASON = (
    "BLOCKED: raw `gh pr merge` / `glab mr merge` on a teatree-managed repo — "
    "an out-of-band merge bypasses the FSM coherence mechanism (ledger update, "
    "MergeClear validation, SHA-binding, privacy/AI-signature scan, mark_merged). "
    "Use the sanctioned keystone transition `t3 <overlay> ticket merge <clear_id>` "
    "(BLUEPRINT §17.1 invariant 8 / §17.4). If this repo is genuinely not "
    "teatree-managed, name the target explicitly with `--repo <owner>/<repo>` (or "
    "a full forge URL) so the gate classifies it from the command itself and never "
    "consults the cwd. kill-switch: `t3 <overlay> gate raw-merge disable`."
)


def _is_raw_merge_api_write(command: str) -> bool:
    """Whether *command* is a raw forge REST WRITE to a merge endpoint.

    Delegates to :func:`teatree.hooks.raw_merge_detect.is_raw_merge_api_write` —
    the SAME leaf the shared hard-deny registry (Lane B) uses, so the two lanes
    classify the merge API write identically. Fails CLOSED (treats the command as
    a possible merge write) on any import error so a broken environment cannot
    weaken the gate; the cwd-managed check then blocks on uncertainty.
    """
    try:
        with _teatree_src_on_path():
            from teatree.hooks import raw_merge_detect  # noqa: PLC0415 (lazy src-bootstrap import)

            return raw_merge_detect.is_raw_merge_api_write(command)
    except Exception:  # noqa: BLE001 (fail-closed: a broken import must not weaken the merge gate)
        return True


def _invokes_raw_merge_subcommand(command: str) -> bool:
    """Whether ``command`` INVOKES ``gh pr merge`` / ``glab mr merge`` as an executed program.

    Delegates to the action-aware :mod:`teatree.hooks.raw_merge_detect`, which
    fires only when the merge subcommand sits at a command position — never when
    the phrase appears inside a heredoc body, a quoted argument, an
    ``echo``/``printf`` string, or a ``#`` comment (#2387). Fails CLOSED (treats
    the command as a possible merge) on any import error so a broken environment
    cannot weaken the gate; the cwd-managed check then blocks on uncertainty.
    """
    try:
        with _teatree_src_on_path():
            from teatree.hooks import raw_merge_detect  # noqa: PLC0415 — deferred: cold-hook import

            return raw_merge_detect.invokes_raw_merge_subcommand(command)
    except Exception:  # noqa: BLE001 — crash-proof hook: any failure degrades silently, never breaks the tool call
        return True


def handle_block_out_of_band_merge(data: dict) -> bool:
    """Block a raw/REST-API/graphql merge form aimed at a teatree-managed repo.

    Three bypass vectors. The literal subcommand (``gh pr merge`` / ``glab mr
    merge``) is matched action-aware by :func:`_invokes_raw_merge_subcommand`
    (a real invocation, not a heredoc/echo/comment — #2387). The REST-API form
    (``gh api .../pulls/<n>/merge -X PUT``) is matched by
    :func:`_is_raw_merge_api_write` (a GET read is NOT denied). A graphql merge
    mutation (mergePullRequest / enablePullRequestAutoMerge / mergeBranch) has an
    unresolvable node-id target, so it is blocked (fail-closed).

    Otherwise classification keys on the merge TARGET via a tri-state
    (:func:`merge_target_managed_state`): a managed target is BLOCKED regardless of
    cwd; a confidently-unmanaged target is ALLOWED on its own evidence; only when NO
    target parses does the cwd-keyed fallback (#126) run (#3343).
    """
    if data.get("tool_name") != "Bash":
        return False
    command = data.get("tool_input", {}).get("command", "")
    # No command, or the kill-switch (`t3 <overlay> gate raw-merge disable`) is off → nothing to gate.
    if not command or not _teatree_bool_setting("out_of_band_merge_gate_enabled", default=True):
        return False
    # Matches the mutation name in argv only: a query loaded from a file or stdin
    # (`gh api graphql -F query=@file` / `--input`) moves the text out of argv and
    # is NOT inspected — an accepted residual, not a silent miss.
    if _GLAB_GH_API_RE.search(command) and _GRAPHQL_MERGE_MUTATION_RE.search(command):
        return _fail_open_or_deny(data, _OUT_OF_BAND_MERGE_REASON)
    if not _invokes_raw_merge_subcommand(command) and not _is_raw_merge_api_write(command):
        return False
    # Tri-state target classification (#3343): a managed target → BLOCK regardless
    # of cwd; a confidently-UNMANAGED target → ALLOW on its own evidence, never
    # keyed off cwd (the bug: a non-git cwd forced a deny on a classifiable target).
    # Only a NON-resolvable target (None) consults the cwd fallback (#126), allowing
    # a confidently-unmanaged cwd (a non-classifiable cwd fails safe to BLOCK).
    target_state = merge_target_managed_state(command, _overlay_managed_repo_signals()[0])
    if target_state is None:
        cwd = _resolve_cwd_repo(data)
        target_state = not (cwd is not None and _cwd_is_teatree_managed(cwd) is False)
    if target_state is False:
        return False
    return _fail_open_or_deny(data, _OUT_OF_BAND_MERGE_REASON)


# ── PreToolUse: mirror-question-to-slack ─────────────────────────────
#
# The Slack TRANSPORT (open DM, post message, channel cache, question text)
# lives in the ``teatree.hooks.slack_mirror`` leaf, which posts through the
# hardened ``SlackHttpClient`` (#1110) instead of the raw ``urllib`` this
# router carried. The leaf is a pure ``teatree.hooks`` (platform-layer) leaf:
# it must not import ``teatree.backends.slack`` / ``teatree.core`` (a backwards
# layer edge tach forbids), so the router — which lives outside ``src`` and may
# touch the domain — builds the Slack ``post`` and the active-DM-thread resolver
# here and INJECTS them into the leaf. The router keeps the ROUTING decision
# (which present-/away-mode arm fires, the DeferredQuestion capture); these thin
# wrappers preserve the ``patch.object(router, "_perform_slack_post" /
# "_slack_config_from_toml" / "_read_dm_channel_cache")`` seam the handler tests
# intercept.


def _active_dm_thread_for_channel(channel: str) -> str:
    """Resolve the user's active DM thread for ``channel`` from ``IncomingEvent``.

    Threads the mirrored question under the conversation the user is already in
    instead of opening a new top-level message. Fail-open: any bootstrap or DB
    error yields ``""`` (post at root) so the hook stays crash-proof.
    """
    if not channel or not bootstrap_teatree_django():
        return ""
    try:
        from teatree.core.models import IncomingEvent  # noqa: PLC0415 — deferred: ORM import needs the app registry

        return IncomingEvent.objects.active_dm_thread(channel=channel)
    except Exception:  # noqa: BLE001 — crash-proof hook: any failure degrades silently, never breaks the tool call
        return ""


def _slack_config_from_toml() -> tuple[str, str] | None:
    from teatree.hooks.slack_mirror import slack_config_from_registry  # noqa: PLC0415 — deferred: cold-hook import

    return slack_config_from_registry()


def _perform_slack_post(slack_cfg: tuple[str, str], questions: list[dict]) -> str:
    from teatree.hooks.slack_mirror import perform_slack_post  # noqa: PLC0415 — deferred: cold-hook import

    return perform_slack_post(
        slack_cfg,
        questions,
        poster=_slack_http_poster(),
        resolve_thread=_active_dm_thread_for_channel,
        enrich_audio=build_dm_audio_enricher(slack_enabled=_speak_settings()[1]),
    )


def _read_dm_channel_cache(user_id: str) -> str:
    from teatree.hooks.slack_mirror import read_dm_channel_cache  # noqa: PLC0415 — deferred: cold-hook import

    return read_dm_channel_cache(user_id)


def _post_question_to_slack(data: dict) -> None:
    questions = data.get("tool_input", {}).get("questions", [])
    if not questions:
        return
    slack_cfg = _slack_config_from_toml()
    if slack_cfg is None:
        return
    _perform_slack_post(slack_cfg, questions)


def handle_mirror_question_to_slack(data: dict) -> bool:
    """Mirror an ``AskUserQuestion`` to Slack; deny a loop-driven one (#1174).

    Runs LAST in the PreToolUse chain. Three arms — live user turn / attended
    non-owner turn: capture, mirror, return ``False`` so the question renders
    in-client (#2058 slides the live window forward on the live arm, the #189
    escape); loop-driven / autonomous turn: capture a generation-stamped
    mirror-linked ``DeferredQuestion``, deduped against a harness retry of the
    SAME denied call, then deny so the agent narrates the deferral and proceeds
    — the answer arrives later via ``additionalContext``.

    All three arms record the question (#3642) so a Slack reply always has a live
    generation to bind and an in-client answer resolves it via
    :func:`handle_resolve_answered_question` — neither surface can apply an answer
    the other already took.
    """
    if data.get("tool_name") != "AskUserQuestion":
        return False
    live = _is_live_user_turn(data)
    if live or not _session_drives_loop(str(data.get("session_id", ""))):
        if live:
            # #2058: an already-live turn rendering in-client is fresh evidence the user
            # is still driving, so the NEXT question in the same walk-through stays live
            # across an intervening notification turn (which never stamps a heartbeat).
            _refresh_live_turn(data)
        if _capture_and_defer_question(data) is None:
            _post_question_to_slack(data)
        return False
    if not str(_first_question(data).get("question", "")).strip():
        _post_question_to_slack(data)
        return False
    queue_id = _capture_and_defer_question(data, dedupe=True)
    if queue_id is None:
        # Teatree unavailable — fail open so the in-client modal renders.
        return False
    reason = (
        f"Your question was captured durably as DeferredQuestion #{queue_id} and mirrored to the "
        "user's Slack DM. A loop-driven AskUserQuestion cannot block here — the suspended session "
        "has no path to receive a Slack reply. Proceed with any work that does not depend on the "
        "answer; the user's reply will surface in a future turn's additionalContext."
    )
    return emit_pretooluse_deny(reason)


def _mirror_question_to_slack(question: dict) -> tuple[str, str]:
    """Post the single recorded question to the user's Slack DM; return ``(ts, channel)``.

    Fail-open: any Slack/IO error yields ``("", "")`` so the deny is never blocked.
    """
    if not question:
        return "", ""
    slack_cfg = _slack_config_from_toml()
    if slack_cfg is None:
        return "", ""
    try:
        ts = _perform_slack_post(slack_cfg, [question])
    except Exception:  # noqa: BLE001 — a Slack failure never blocks the capture/deny.
        return "", ""
    return ts, _read_dm_channel_cache(slack_cfg[1])


def _run_id(data: dict) -> str:
    """Harness run id when the payload exposes one; fall back to session id.

    The (session, run) pair scopes the generation cursor so a Slack reply
    can never cross-apply between two distinct runs sharing a session id.
    """
    for key in ("run_id", "agent_run_id", "tool_use_id"):
        value = str(data.get(key, "")).strip()
        if value:
            return value
    return str(data.get("session_id", ""))


def _options_hash(options: list[dict]) -> str:
    """SHA-256 of canonicalized options — the stable identity a re-ask is matched on."""
    blob = json.dumps(options, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _first_question(data: dict) -> dict:
    questions = data.get("tool_input", {}).get("questions", []) or []
    first = questions[0] if isinstance(questions, list) and questions else {}
    return first if isinstance(first, dict) else {}


def _capture_and_defer_question(data: dict, *, dedupe: bool = False) -> int | None:
    """Record one mirror-linked ``DeferredQuestion`` and post it to Slack.

    The single chokepoint every ``AskUserQuestion`` arm calls (#1174). It supersedes
    any pending older-generation row for the same (session, run), posts the question
    to the user's Slack DM capturing the posted ``ts``, and records the row with its
    mirror fields so the reply matcher can bind a later Slack reply to exactly this
    generation. Returns the new row id, or ``None`` when teatree is unavailable (fails
    open — the in-client modal renders).

    *dedupe* (the loop-driven deny arm) makes the row itself the idempotency record: a
    harness retry returns the live row rather than superseding it into a mirrorless twin
    ``live_for_reply`` cannot bind, which silently drops the operator's Slack answer.
    """
    if not bootstrap_teatree_django():
        return None
    try:
        from teatree.core.models.deferred_question import DeferredQuestion  # noqa: PLC0415 — deferred: ORM/app-registry
    except Exception:  # noqa: BLE001 — crash-proof hook: any failure degrades silently, never breaks the tool call
        return None
    first = _first_question(data)
    question_text = str(first.get("question", "")).strip()
    if not question_text:
        return None
    options = first.get("options", []) if isinstance(first.get("options"), list) else []
    session_id = str(data.get("session_id", ""))
    run_id = _run_id(data)
    marker = denied_question_row_marker(session_id, denied_question_dedupe_key(first)) if dedupe else ""
    try:
        row = DeferredQuestion.pending().filter(dedupe_marker=marker).first() if marker else None
        generation = DeferredQuestion.next_generation(session_id=session_id, run_id=run_id)
        if row is None:
            for prior in DeferredQuestion.pending().filter(session_id=session_id, run_id=run_id):
                prior.mark_stale("superseded by newer question")
    except Exception:  # noqa: BLE001 — crash-proof hook: any failure degrades silently, never breaks the tool call
        return None
    if row is None:
        slack_ts, slack_channel = _mirror_question_to_slack(first)
        try:
            row = DeferredQuestion.record(
                question_text,
                options_json=json.dumps(options) if options else "",
                session_id=session_id,
                tool_use_id=str(data.get("tool_use_id", "")),
                slack_ts=slack_ts,
                slack_channel=slack_channel,
                options_hash=_options_hash(options),
                generation=generation,
                run_id=run_id,
                dedupe_marker=marker,
            )
        except Exception:  # noqa: BLE001 — crash-proof hook: any failure degrades silently, never breaks the tool call
            return None
    return int(row.pk)


def _is_live_user_turn(data: dict) -> bool:
    """True when the user typed a prompt THIS turn in this session (#189).

    The user-driven escape for away-mode: ``/checking`` (and "shoot me
    questions from here") work because a question raised on a live user
    turn renders in-client even under a manual-away override — no
    availability flip needed. Crash-proof and FAIL-SAFE: a missing
    ``teatree`` import, an unreadable heartbeat, or any error returns
    ``False`` so an autonomous turn always falls through to the durable
    deferral path (BLUEPRINT §17.1 invariant 9 unweakened).
    """
    if not bootstrap_teatree_django():
        return False
    try:
        from teatree.live_presence import PRESENCE  # noqa: PLC0415 — deferred: cold-hook import after sys.path setup

        return PRESENCE.is_live_user_turn(session_id=str(data.get("session_id", "")))
    except Exception:  # noqa: BLE001 — crash-proof hook: any failure degrades silently, never breaks the tool call
        return False


def _refresh_live_turn(data: dict) -> None:
    """Slide the live-turn window forward when an already-live question renders.

    Keeps a multi-question user-driven walk-through (``/checking``) live across
    an intervening background task-notification turn, which never refreshes the
    presence heartbeat (#2058). Crash-proof and best-effort: any error is
    swallowed so a failed slide never blocks the in-client render. The
    underlying primitive only re-stamps an ALREADY-live same-session turn, so
    this can never promote an autonomous turn to live (invariant 9 intact).
    """
    if not bootstrap_teatree_django():
        return
    try:
        from teatree.live_presence import PRESENCE  # noqa: PLC0415 — deferred: cold-hook import after sys.path setup

        PRESENCE.refresh_live_turn(session_id=str(data.get("session_id", "")))
    except Exception:  # noqa: BLE001 — crash-proof hook: any failure degrades silently, never breaks the tool call
        return


# ── UserPromptSubmit: inject pending-question backlog into context ────────────


def handle_inject_pending_questions(data: dict) -> None:
    """Inject resolved answers and the still-pending backlog into ``additionalContext``.

    Two halves, both fail-open if teatree is unavailable:

    - Apply leg (#1174): every ``DeferredQuestion`` answered (on Slack or
    via ``t3 teatree questions answer``) but not yet delivered is emitted
    as a "your AskUserQuestion was answered — apply it now" line and
    stamped ``applied_at`` (single-use CAS) so it surfaces exactly once.
    This is the success state that closes the loop, and it also delivers
    away-mode answers that previously had no injection path.
    - Backlog leg (#58): the still-pending questions are listed so the
    agent prioritises work that does NOT depend on those answers.
    """
    # Django-free pre-check (#22): skip the ~8s django.setup() on the common
    # empty-backlog turn (the has-work probe short-circuits the boot). Fails OPEN
    # (boots Django) on any unreadable-DB error, so a row is never dropped.
    if not (has_pending_question_work() and bootstrap_teatree_django()):
        return
    try:
        from teatree.core.models.deferred_question import DeferredQuestion  # noqa: PLC0415 — deferred: ORM/app-registry
    except Exception:  # noqa: BLE001 — crash-proof hook: any failure degrades silently, never breaks the tool call
        return
    session_id = str(data.get("session_id", ""))
    try:
        answered = list(DeferredQuestion.answered_not_applied(session_id=session_id)[:5])
    except Exception:  # noqa: BLE001 — crash-proof hook: any failure degrades silently, never breaks the tool call
        answered = []
    for row in answered:
        with contextlib.suppress(Exception):
            if DeferredQuestion.mark_applied(row.pk):
                print(  # noqa: T201 — hook writes its protocol output to stdout
                    f"Your AskUserQuestion (#{row.pk}) was answered by the user on Slack: "
                    f'"{row.answer_text}". Apply it now.'
                )
    try:
        count = DeferredQuestion.pending().count()
        if count == 0:
            return
        rows = list(DeferredQuestion.pending()[:5])
    except Exception:  # noqa: BLE001 — crash-proof hook: any failure degrades silently, never breaks the tool call
        return
    lines = [f"You have {count} deferred question(s) awaiting user answer:"]
    lines.extend(f"  #{row.pk} — {row.question[:120]}" for row in rows)
    print("\n".join(lines))  # noqa: T201 — hook writes its protocol output to stdout


# ── UserPromptSubmit: inject pending Slack-DM backlog into context ─────────────
#
# Inbound half of the Slack ↔ Claude-Code bidirectional bridge (#1014,
# BLUEPRINT §17.1 invariant 2 / §5.6). The user only reads Slack DMs;
# their reply to the overlay bot lands here as a ``PendingChatInjection``
# row. The next ``UserPromptSubmit`` drain reads unconsumed rows for the
# t3-master session and emits them into ``additionalContext`` — the
# agent sees the message as if the user had typed it in chat.


def handle_inject_pending_chat(data: dict) -> None:
    """Append unconsumed Slack-DM messages to the next prompt's ``additionalContext``.

    **Drain eligibility:** ANY interactive Claude Code session that
    receives a ``UserPromptSubmit`` event may drain the queue. The
    original implementation gated on ``_session_owns_loop`` (mirroring
    the §5.6 ``handle_loop_self_pump`` discipline), but the t3-master
    record points at the autonomous ``t3 loop start`` session — which
    never receives ``UserPromptSubmit`` events — so the gate prevented
    the queue from ever draining (32 unconsumed rows observed in
    production). The self-pump owner-gate is correct for self-pump
    (must be singleton); it was the wrong invariant for the inbound
    bridge, where the *whole point* is that the user's queued replies
    must reach an interactive session.

    At-most-once delivery is preserved by primitives other than the
    owner-gate: ``PendingChatInjection.consume()`` is a single-use
    durable transition (``UPDATE … WHERE consumed_at IS NULL``) so a
    concurrent second drain sees the row already stamped and emits
    nothing, and the ``(overlay, slack_ts)`` ``UniqueConstraint``
    deduplicates the ingest side so over-polling is safe.

    Fails open: if teatree is unavailable, just skip — the queue
    survives to the next tick.
    """
    session_id = data.get("session_id", "")
    if not session_id:
        return
    # Django-free pre-check (#22): skip the ~8s django.setup() when the drain
    # queue is empty (the has-work probe short-circuits the boot). Fails OPEN
    # (boots Django) on any unreadable-DB error, so a queued reply is never dropped.
    if not (has_pending_chat_work() and bootstrap_teatree_django()):
        return
    try:
        from teatree.core.models.pending_chat_injection import PendingChatInjection  # noqa: PLC0415 — lazy ORM import
    except Exception:  # noqa: BLE001 — fail open: queue survives to the next tick
        return
    try:
        rows = list(PendingChatInjection.pending())
    except Exception:  # noqa: BLE001 — crash-proof hook: any failure degrades silently, never breaks the tool call
        return
    drained: list[str] = [f"User replied on Slack at {row.slack_ts}: {row.text}" for row in rows if row.consume()]
    if not drained:
        return
    header = f"You have {len(drained)} new Slack DM reply(ies) from the user:"
    print("\n".join([header, *drained]))  # noqa: T201 — hook writes its protocol output to stdout


# ── Stop: enforce-answered-questions gate (#1063) ───────────────────
#
# ``consumed_at`` proves the agent *read* the row into ``additionalContext``;
# it does NOT prove the agent *replied*. Empirically (2026-05-19) the
# drain mechanism worked perfectly for 6 hours while ~22 of 25 user
# questions sat silently ignored — the agent treated the drained content
# as background and continued executing its loop directive. This Stop
# hook is the structural fix: it queries the model's
# ``unanswered_questions_since(1h)`` and emits a prominent
# ``additionalContext`` BLOCKING REMINDER listing each unanswered
# question. The user might genuinely be done, so we deliberately soft-
# block via ``additionalContext`` rather than hard-blocking via
# ``decision: block``.
#
# Hook contract: must be crash-proof (#810 — a Stop hook must NEVER raise
# to the session). A broad boundary guard contains any unexpected error
# to a stderr line and a clean ``None``.

_ANSWERED_GATE_WINDOW_HOURS = 1


def handle_enforce_answered_questions(data: dict) -> bool | None:
    """Emit a BLOCKING REMINDER for user questions still unanswered (#1063).

    Returns ``None`` always — never hard-blocks (the user may have
    genuinely typed "ok thanks" and meant for the turn to end). The
    nag is in ``additionalContext`` so it lands in the NEXT turn's
    system context, deterministically visible.
    """
    try:
        return _enforce_answered_questions(data)
    except Exception as exc:  # noqa: BLE001 — Stop hook must be crash-proof
        print(  # noqa: T201 — hook stderr is the module's logging channel
            f"[hook_router] enforce-answered-questions skipped (unexpected error: {exc})",
            file=sys.stderr,
        )
        return None


def _enforce_answered_questions(data: dict) -> bool | None:
    if data.get("stop_hook_active"):
        return None
    if not bootstrap_teatree_django():
        return None
    try:
        from datetime import timedelta  # noqa: PLC0415 — deferred: off the fast hook's load path

        from teatree.core.models.pending_chat_injection import PendingChatInjection  # noqa: PLC0415 — lazy ORM import
    except Exception:  # noqa: BLE001 — fail open: nag re-tries next turn
        return None
    try:
        rows = PendingChatInjection.unanswered_questions_since(timedelta(hours=_ANSWERED_GATE_WINDOW_HOURS))
    except Exception:  # noqa: BLE001 — crash-proof hook: any failure degrades silently, never breaks the tool call
        return None
    if not rows:
        return None
    bullets = [f"  - ts={row.slack_ts}: {row.text.strip()}" for row in rows]
    body = (
        f"BLOCKING REMINDER — {len(rows)} user question(s) from the last hour are unanswered. "
        "The Slack-DM drain stamped consumed_at but you have not replied. "
        "The turn cannot end cleanly until each question is answered (post via "
        "`notify_user(..., kind=NotifyKind.ANSWER, idempotency_key='answer-<short>-<ts>')` "
        "or `t3 teatree pending_chat mark-answered <ts>`).\n"
        "Unanswered:\n" + "\n".join(bullets)
    )
    # Stop hooks may NOT carry ``hookSpecificOutput.additionalContext`` —
    # the Claude Code schema reserves that field for ``UserPromptSubmit`` /
    # ``PostToolUse`` / ``PostToolBatch``. Emitting it for ``Stop`` makes
    # the validator reject the JSON ("Hook JSON output validation failed —
    # (root): Invalid input") and the nag is lost. The schema-valid soft-
    # block channel is the top-level ``systemMessage`` string, which
    # surfaces the body to the agent without hard-blocking the turn.
    json.dump({"systemMessage": body}, sys.stdout)
    # Return True to break the Stop chain — we want the systemMessage
    # nag delivered intact, and we want to preempt any subsequent handler
    # (notably loop_self_pump) that would also write to stdout and either
    # corrupt the JSON or override our soft-block with a hard-block
    # continuation directive. Soft-block intent is preserved by emitting
    # only ``systemMessage``, never ``decision: block``.
    return True


# ── Consideration gate (#1129): promote framework-shaped edits ──────
#
# Every session that edits personal config the framework should ship
# (e.g. ``~/.claude/settings.json``, ``~/.claude/hooks.json``, personal
# ``CLAUDE.md`` behavioural rules) must answer "should this be a teatree
# feature?" before the turn declares done. Prose-only enforcement loses
# (see retro skill § 9 "Consolidation over Drift"); this gate makes the
# scan deterministic.
#
# The classifier is path-based and conservative. Three classes:
#
#   (P) Promote — personal agent config a teatree installation should
#       wire automatically. The gate fires unless the assistant turn
#       references a teatree issue (``souliane/teatree#NNNN`` or bare
#       ``#NNNN``) OR a later iteration downgrades the path.
#   (K) Keep    — genuine personal preference (memory entries, shell
#       rc, terminal config). The gate stays silent.
#   None        — path lives outside the personal-config corners (or
#       inside the framework itself). The gate has nothing to say.
#
# Class (C) "documented config" is not encoded here yet — overlapping
# heuristics with (P) make false positives noisy. The retro skill
# already covers (C) in its consolidation pass; the Stop gate focuses
# on the loudest signal first.

_TEATREE_ISSUE_REF = re.compile(
    r"(?:souliane/teatree)?#(\d{2,})\b",
    flags=re.IGNORECASE,
)

_PROMOTE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Agent-harness config files that ship behaviour.
    re.compile(r"/\.(claude|codex|cursor|copilot)/settings(\.local)?\.json$"),
    re.compile(r"/\.(claude|codex|cursor|copilot)/hooks\.json$"),
    # Personal behavioural instructions (CLAUDE.md / AGENTS.md at the
    # harness root, not inside a project repo).
    re.compile(r"/\.(claude|codex|cursor|copilot)/(CLAUDE|AGENTS)\.md$"),
)

_KEEP_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Memory entries and todos are session state, not framework behaviour.
    re.compile(r"/\.(claude|codex|cursor|copilot)/projects/.*/memory/"),
    re.compile(r"/\.(claude|codex|cursor|copilot)/todos/"),
    re.compile(r"/\.(claude|codex|cursor|copilot)/statsig/"),
    re.compile(r"/\.(claude|codex|cursor|copilot)/.*\.log$"),
    # Shell, terminal, vcs user prefs.
    re.compile(r"/\.(zshrc|bashrc|profile|zprofile|zshenv|bash_profile)$"),
    re.compile(r"/\.(gitconfig|tmux\.conf|inputrc|vimrc)$"),
)


def classify_session_edit(file_path: str) -> str | None:
    """Classify an edited path as ``"P"`` (promote), ``"K"`` (keep), or ``None``.

    Conservative path-based heuristic — see the consideration-gate
    block above for the (P)/(K)/None contract. ``None`` is the silent
    default: the framework only nags on paths it has explicit signal
    for.
    """
    if not file_path:
        return None
    # Keep patterns win over promote when both could match the path —
    # an edit to ``~/.claude/projects/<p>/memory/MEMORY.md`` is keep,
    # not promote.
    for pattern in _KEEP_PATTERNS:
        if pattern.search(file_path):
            return "K"
    for pattern in _PROMOTE_PATTERNS:
        if pattern.search(file_path):
            return "P"
    return None


# ── Stop: speak-on-stop arm (local == all, #2060) ───────────────────────────


def _speak_settings() -> tuple[str, bool]:
    """Read the global ``speak`` DB row → ``(local, slack)`` (#2060, DB-home).

    The hook-side mirror of :func:`teatree.config.speak.resolve_speak`. ``speak`` is
    DB-home (#1775): the Stop hook cannot cheaply boot the Django config, so it reads
    the same ``ConfigSetting`` store via the Django-free :mod:`teatree.config.cold_reader`
    — a stored JSON dict ``{"local": ..., "slack": ...}``, else the defaults
    (``"off", False``). ``local`` is the :class:`~teatree.types.LocalPlayback` value
    (``off``/``dm``/``all``). Best-effort: a missing DB / row / malformed value yields
    the defaults so the Stop arm stays silent unless the user opted in. A
    ``[teatree.speak]`` TOML value is ignored on read.
    """
    from typing import cast  # noqa: PLC0415 — deferred: off the fast hook's load path

    from teatree.config import cold_reader  # noqa: PLC0415 — Django-free DB read on the pre-Django Stop path

    raw = cold_reader.read_setting("speak")
    if isinstance(raw, dict):
        subtable = cast("dict[str, Any]", raw)
        local = subtable.get("local")
        return (
            local.strip().lower() if isinstance(local, str) else "off",
            bool(subtable.get("slack", False)),
        )
    return "off", False


def handle_speak_all_on_stop(data: dict) -> None:
    """Speak the in-client turn on the speakers when ``local == all`` (#2060).

    The Stop-hook arm fires its detached ``t3 speak`` IFF ``local == all`` —
    in-client turns are never Slack messages, so the ``slack`` attach is
    irrelevant and there is no double-play to suppress. The toml pre-check
    keeps the fast hook from spawning Django on every Stop. Returns ``None``
    unconditionally (a side-effect handler, never a decision) and is
    crash-proof.
    """
    try:
        local, _slack = _speak_settings()
        if local != "all":
            return
        if shutil.which("say") is None or shutil.which("t3") is None:
            return
        turn = _last_assistant_turn(data.get("transcript_path", ""))
        if turn is None:
            return
        text = turn[0].strip()
        if not text:
            return
        overlay = os.environ.get("T3_OVERLAY_NAME", "")
        argv = [shutil.which("t3") or "t3", "speak", text]
        if overlay:
            argv.extend(["--overlay", overlay])
        subprocess.Popen(  # noqa: S603 — detached, fire-and-forget; speak is best-effort
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as exc:  # noqa: BLE001 — Stop hook must be crash-proof
        print(  # noqa: T201 — hook stderr is the module's logging channel
            f"[hook_router] speak-on-stop skipped (unexpected error: {exc})",
            file=sys.stderr,
        )
    return


# ── Closure-verb re-verify advisory (#1448) ─────────────────────────────────
#
# The orchestrator has claimed a closure ("merged #N", "closed !N", "confirmed
# superseded") WITHOUT verifying the id's live state in the same turn (2x
# recurrence). A turn-level check catches it. But a turn-inspecting hook that
# over-fires is dangerous — a sibling skill-loading gate over-fired and
# deadlocked the loop (#1567). So this is WARN-ONLY: it emits a top-level
# ``systemMessage`` advisory and NEVER denies, exactly like the bare-reference
# and consideration Stop advisories. Zero deadlock risk; a missed nudge is
# cheaper than a false block on a legitimate or already-verified closure.
#
# It fires only when a HIGH-confidence closure claim re-cites an id AND no
# same-turn state-check tool_use touched that id. The detection lives in the
# pure ``closure_reverify_scanner`` module (tuned for precision); this handler
# is the thin transcript-reading wrapper, fail-safe-to-silent on any error.


def handle_closure_reverify_stop(data: dict) -> bool | None:
    """WARN when the final turn claims a closure with no same-turn state check.

    Soft sibling of the structured-question and bare-reference Stop gates.
    Emits a top-level ``systemMessage`` advisory and returns ``True`` to break
    the chain (preserving the single-stdout JSON shape) ONLY when a
    high-confidence closure claim re-cites an id that no same-turn state-check
    tool_use touched. Never denies — over-firing here would risk the #1567
    deadlock, so WARN-only is the deliberate posture.

    Fail-safe-to-silent: any malformed input or missing transcript returns
    ``None`` so the Stop chain is never crashed.
    """
    src_dir = Path(__file__).resolve().parents[2] / "src"
    added = False
    try:
        if str(src_dir) not in sys.path:
            sys.path.insert(0, str(src_dir))
            added = True
        return _run_closure_reverify_stop(data)
    except Exception:  # noqa: BLE001 — Stop hook must be crash-proof
        return None
    finally:
        if added:
            with contextlib.suppress(ValueError):
                sys.path.remove(str(src_dir))


def _run_closure_reverify_stop(data: dict) -> bool | None:
    from teatree.hooks import closure_reverify_scanner  # noqa: PLC0415 — deferred: cold-hook import

    turn = _last_assistant_turn(data.get("transcript_path", ""))
    if turn is None:
        return None
    tool_commands = current_turn_tool_commands(data.get("transcript_path", ""))
    unverified = closure_reverify_scanner.find_unverified_closures(turn[0], tool_commands)
    if not unverified:
        return None
    json.dump({"systemMessage": closure_reverify_scanner.format_warn_message(unverified)}, sys.stdout)
    return True


def handle_consideration_gate(data: dict) -> bool | None:
    """Emit a CONSIDERATION GATE reminder when promotable edits land (#1129).

    The gate scans the current turn's ``Edit`` / ``Write`` /
    ``NotebookEdit`` tool uses, classifies each, and emits an
    ``additionalContext`` block when one or more land in class (P) AND
    the assistant's text in the same turn does not already reference a
    teatree issue.

    Soft block only: never emits ``decision: block``. The next turn
    sees the nag in system context and is expected to either open a
    teatree issue or justify the divergence in plain text (which the
    next gate fire will pick up as a reference).
    """
    try:
        return _consideration_gate(data)
    except Exception as exc:  # noqa: BLE001 — Stop hook must be crash-proof
        print(  # noqa: T201 — hook stderr is the module's logging channel
            f"[hook_router] consideration-gate skipped (unexpected error: {exc})",
            file=sys.stderr,
        )
        return None


def _consideration_gate(data: dict) -> bool | None:
    if data.get("stop_hook_active"):
        return None
    transcript_path = data.get("transcript_path") or ""
    if not transcript_path:
        return None
    edits = _current_turn_edits(transcript_path)
    if not edits:
        return None
    # Dedupe while preserving order.
    seen: set[str] = set()
    promotable: list[str] = []
    for path in edits:
        if path in seen:
            continue
        seen.add(path)
        if classify_session_edit(path) == "P":
            promotable.append(path)
    if not promotable:
        return None
    # An issue reference in the assistant's turn text is the spec's
    # "open a teatree issue" half — gate clears.
    if _TEATREE_ISSUE_REF.search(_current_turn_assistant_text(transcript_path)):
        return None
    bullets = "\n".join(f"  - {path}" for path in promotable)
    body = (
        f"CONSIDERATION GATE — {len(promotable)} edit(s) this turn landed on personal "
        "agent config that teatree should arguably ship for every install. "
        "Before declaring done, decide one of:\n"
        "  1. Promote — open a teatree issue (link it as `souliane/teatree#NNNN` "
        "or bare `#NNNN`) so this behaviour ships in the framework.\n"
        "  2. Justify keep-personal — say in plain text why this edit is genuinely "
        "user-specific (theme, voice, paths) and not a missing framework feature.\n"
        "Promotable paths:\n" + bullets
    )
    # Stop schema rejects ``hookSpecificOutput.additionalContext`` —
    # ``additionalContext`` is reserved for ``UserPromptSubmit`` /
    # ``PostToolUse`` / ``PostToolBatch``. Soft-block via top-level
    # ``systemMessage`` (schema-valid; non-decision; visible to the agent).
    json.dump({"systemMessage": body}, sys.stdout)
    # Return True to break the Stop chain — preserves the JSON shape and
    # preempts the loop self-pump (which would override our soft-block
    # with a continuation directive).
    return True


# ── Router ──────────────────────────────────────────────────────────


_HANDLERS: dict[str, list] = {
    "UserPromptSubmit": [
        handle_clear_classifier_deny_marker,
        handle_reset_turn_tool_budget,
        handle_record_presence,
        handle_record_operator_message,
        handle_enforce_loop_on_prompt,
        handle_todo_freshness_nudge,
        handle_inject_pending_questions,
        handle_inject_pending_chat,
        handle_user_prompt_submit,
        # LAST: cold-tier memory recall injection (#2746) — runs after skill
        # loading so it never delays the load-first suggestion.
        handle_recall_cold_memory,
    ],
    "PreToolUse": [
        handle_allow_classifier_relax_settings_write,
        handle_block_edit_before_planned,
        handle_block_config_overwrite,
        handle_protect_default_branch,
        handle_block_main_clone_mutation,
        handle_block_second_branch,
        handle_block_interactive_authoring,
        handle_block_self_dm_via_mcp,
        handle_block_mcp_slack_write,
        handle_quote_scanner_pretool,
        handle_dispatch_prompt_quote_scanner,
        handle_dispatch_admission,
        handle_banned_terms_pretool,
        handle_block_verbatim_operator_paste,
        handle_enforce_skill_loading,
        handle_block_direct_commands,
        handle_block_git_add_all,
        handle_block_raw_pid_kill,
        handle_block_unbounded_wait,
        handle_block_secret_file_print,
        handle_block_out_of_band_merge,
        handle_block_unknown_repo_push,
        handle_block_raw_review_post,
        handle_validate_mr_metadata,
        handle_block_glab_stale_base_remote,
        handle_block_self_reviewer_assign,
        handle_block_ai_signature,
        handle_block_uncovered_diff,
        handle_enforce_orchestrator_boundary,
        handle_enforce_orchestrator_investigation_boundary,
        handle_warn_merged_detection_probe,
        handle_warn_batched_questions,
        handle_mirror_question_to_slack,
        handle_orchestrator_turn_budget_nudge,
    ],
    "PostToolUse": [
        handle_track_classifier_denial,
        handle_track_active_repo,
        handle_track_skill_usage,
        handle_track_cron_jobs,
        handle_read_dedup,
        handle_track_agents,
        handle_resolve_answered_question,
    ],
    "TaskCreated": [handle_dispatch_prompt_quote_scanner_on_task_create],
    "InstructionsLoaded": [handle_track_skill_usage],
    "SessionStart": [handle_session_start_bootstrap],
    "PreCompact": [handle_pre_compact],
    # #845: PostCompact deliberately NOT registered — the harness has no
    # hookSpecificOutput entry for it and discards its output. Recovery
    # runs in handle_session_start_bootstrap on source=="compact".
    "SessionEnd": [handle_session_end, handle_session_end_loop_registry, handle_session_end_self_pump],
    "Stop": [
        handle_classifier_deny_stop_gate,
        handle_enforce_structured_question,
        handle_answer_first_gate,
        handle_completion_claim_gate,
        handle_unbacked_claim_gate,
        handle_standing_goal_stop,
        handle_enforce_answered_questions,
        handle_closure_reverify_stop,
        handle_consideration_gate,
        handle_speak_all_on_stop,
        handle_stop_snapshot_slot,
        handle_loop_self_pump,
    ],
    "SubagentStop": [handle_subagent_stop_no_commit, handle_subagent_stop_track_agent, handle_subagent_stop_release],
}

# Events whose block/deny is carried by a TOP-LEVEL ``decision`` JSON object on
# stdout and read by the harness ONLY at exit code 0. For these, exiting 2 is a
# *blocking error*: the harness ignores stdout (and the ``decision: block`` JSON
# in it) and feeds STDERR back to Claude — so an exit-2 block discards the reason
# and surfaces an empty "No stderr output" failure. PreToolUse / TaskCreated are
# the exceptions: their deny is only honoured at exit code 2 (#1447), so they are
# deliberately absent here and keep exiting 2.
_JSON_DECISION_EVENTS: frozenset[str] = frozenset(
    {"Stop", "SubagentStop", "UserPromptSubmit", "PostToolUse", "PreCompact"},
)


def main() -> None:
    global _CURRENT_EVENT, _CURRENT_DATA  # noqa: PLW0603 — per-process context for the deny circuit breaker.
    args = _parse_args()
    handlers = _HANDLERS.get(args.event, [])
    if not handlers:
        return

    data = _read_input()
    if not data:
        return

    _CURRENT_EVENT = args.event
    _CURRENT_DATA = data

    deny_emitted = False
    for handler in handlers:
        # A handler's own crash is cannot-evaluate, NOT a content deny: skip the
        # broken gate and continue the chain so a handler whose internal
        # fail-open is incomplete can neither (a) surface its crash as a deny
        # that hard-blocks the tool, nor (b) disable every downstream gate. The
        # diagnostic goes to stderr (never stdout) so it cannot be read as a
        # deny payload. A truthy return is a DECISION that stops the chain (to
        # avoid writing multiple JSON objects to stdout): ``True`` is a deny
        # (exit 2), the ``Verdict.ALLOW`` sentinel is an explicit allow (exit 0,
        # #3). ``None`` / ``False`` are "no decision" — the chain continues.
        try:
            verdict = handler(data)
        except Exception:  # noqa: BLE001 — crash-proof router: a broken gate fails open, never denies.
            traceback.print_exc(file=sys.stderr)
            continue
        if verdict:
            deny_emitted = verdict is True
            break

    # A PreToolUse call that ran the whole chain without a deny is genuine
    # progress: reset the deny-streak so only CONSECUTIVE identical denials
    # accumulate in the circuit breaker. A ``Verdict.ALLOW`` decision leaves
    # ``deny_emitted`` False, so a sanctioned allow resets the streak too.
    if args.event == "PreToolUse" and not deny_emitted:
        _reset_deny_streak(data.get("session_id", ""))

    # Exit-code contract is per-event. PreToolUse / TaskCreated denies are only
    # honoured at exit code 2 (#1447) and their reason rides ``hookSpecificOutput``
    # / ``continue:false`` on stdout, which the harness reads even at exit 2.
    # A ``Verdict.ALLOW`` decision must NOT exit 2: the harness honours a PreToolUse
    # allow only at exit 0 with the nested envelope, so ``deny_emitted`` gates the
    # exit-2 branch and an allow falls through to the exit-0 default (#3).
    # Stop / SubagentStop and the other top-level-``decision`` events INVERT this:
    # exit 2 is a blocking error that makes the harness discard the stdout JSON
    # and read stderr instead — so a Stop block must exit 0 to let its
    # ``{"decision":"block","reason":...}`` reach the agent. Exiting 2 there was
    # the "Stop hook fails with No stderr output" defect.
    if deny_emitted and args.event not in _JSON_DECISION_EVENTS:
        sys.exit(2)


if __name__ == "__main__":
    main()
