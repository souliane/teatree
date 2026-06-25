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
import dataclasses
import hashlib
import json
import os
import re
import shutil
import subprocess  # noqa: S404
import sys
import tempfile
import time
import traceback
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import ModuleType

# Put this script's own dir on sys.path so the bare sibling-module imports
# resolve whether the router runs as a script (the live hook) or is imported
# as ``hooks.scripts.hook_router`` in a subprocess test.
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
# Alias the bare ``hook_router`` name to this module object so a sibling's
# ``from hook_router import STATE_DIR`` and a test's ``import
# hooks.scripts.hook_router`` resolve the SAME globals (a test patching
# ``STATE_DIR`` must reach what the sibling reads). Unconditional, so it holds
# regardless of whether the parent dir was already on ``sys.path``.
if "hook_router" not in sys.modules:
    sys.modules["hook_router"] = sys.modules[__name__]

from availability_away_probe import resolved_away_mode as resolved_away_mode_stdlib
from banned_terms_deny import emit_banned_term_deny
from banned_terms_marker import resolve_marker as _resolve_banned_terms_marker
from completion_claim_gate import handle_completion_claim_gate
from config_overwrite_guard import handle_block_config_overwrite
from django_bootstrap import bootstrap_teatree_django
from loop_registrations import emit_loop_registrations, is_bare_loop_tick_prompt, loop_name_from_prompt
from loop_state_self_pump_gate import db_loop_state_suppresses_self_pump
from mr_cli_fields import extract_cli_mr_fields, extract_mr_target_repo
from no_self_reviewer_assign import handle_block_self_reviewer_assign
from question_gates import FENCED_CODE_RE, handle_warn_batched_questions, is_user_directed_question
from state_files import append_line, read_lines
from subagent_skill_gate import is_file_safe, unreferenced_demand_reason
from turn_inspect import current_turn_tool_commands
from unknown_repo_push_gate import handle_block_unknown_repo_push

STATE_DIR = Path(
    os.environ.get(
        "TEATREE_CLAUDE_STATUSLINE_STATE_DIR",
        os.environ.get("T3_HOOK_STATE_DIR", "/tmp/claude-statusline"),  # noqa: S108
    )
)

# Per-invocation context shared with the deny circuit breaker. Each hook event
# is a fresh ``python3`` process (one per tool call), so these globals are set
# once by ``main`` and never carry across calls. The breaker reads them so the
# centralised ``emit_pretooluse_deny`` chokepoint can fingerprint a deny against
# the session without threading ``data`` through 15+ existing call sites.
_CURRENT_EVENT: str = ""
_CURRENT_DATA: dict = {}

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

# Commands that are legitimate t3 CLI invocations — never block these.
# `uv run t3 ...` is intentionally NOT whitelisted here: it is caught by the
# blocked-commands list below so agents switch to the globally-installed t3.
_T3_CMD_PREFIX_RE = re.compile(
    r"^(?:\w+=\S+\s+)*t3\s",
)

# Read-only commands that may mention infrastructure tools as arguments
# (e.g. grep for 'playwright', echo about manage.py) — never block these.
_READONLY_CMD_PREFIX_RE = re.compile(
    r"^(?:echo|printf|cat|grep|rg|awk|sed|head|tail|less|wc|file|#)",
)

# Forbidden command patterns → deny messages.  Each entry is
# (compiled regex matching the Bash command, human-readable deny reason).
# Patterns that match a VALUE or CONFIG TOKEN that can legitimately appear
# inside a quoted argument in a real bypass (e.g. ``git -c "core.hooksPath=x"``
# or ``git push -o "merge_request.merge_when_pipeline_succeeds"``).  These
# must be scanned against the RAW command so quoting cannot evade them.
_RAW_SCAN_BLOCKED: list[tuple[re.Pattern[str], str]] = [
    (
        # F3: ``git -c core.hooksPath=…`` redirects git's hooks directory,
        # silencing all hooks — semantically identical to ``--no-verify``.
        # The value (e.g. ``/dev/null``) can appear inside single- or
        # double-quoted args: ``git -c "core.hooksPath=/dev/null"`` is a real
        # bypass and must be caught against the raw command.
        re.compile(r"\bgit\b.*-c\s+['\"]?core\.hooksPath\s*=", re.IGNORECASE),
        (
            "BLOCKED: `git -c core.hooksPath=…` bypasses git hooks "
            "(equivalent to `--no-verify`) — fix the hook failure instead."
        ),
    ),
    (
        # F8: ``git push -o merge_request.merge_when_pipeline_succeeds`` schedules
        # a GitLab auto-merge, bypassing the FSM keystone transition
        # (``t3 <overlay> ticket merge``). The ``--push-option=`` long form is
        # equivalent.  The push-option value can appear quoted on the command
        # line, so scan raw.
        re.compile(
            r"\bgit\s+push\b.*"
            r"(?:-o\s+['\"]?merge_request\.merge_when_pipeline_succeeds"
            r"|--push-option=['\"]?merge_request\.merge_when_pipeline_succeeds)"
        ),
        (
            "BLOCKED: `git push -o merge_request.merge_when_pipeline_succeeds` "
            "schedules an auto-merge bypassing the FSM keystone — "
            "use `t3 <overlay> ticket merge` instead."
        ),
    ),
]

# Patterns that match a TOOL INVOCATION that, in any real command, appears
# unquoted at command position.  These are scanned against a quote-stripped
# copy of the command so a tool name that merely appears inside a quoted
# commit message / grep argument does not false-block.
_QUOTE_STRIPPED_BLOCKED: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"\.venv/bin/"),
        "BLOCKED: `.venv/bin/...` — use `uv run` instead so the resolved environment matches `pyproject.toml`.",
    ),
    (
        re.compile(r"manage\.py\s+runserver"),
        "BLOCKED: `manage.py runserver` — use `t3 <overlay> worktree start` instead.",
    ),
    (
        re.compile(r"manage\.py\s+migrate"),
        "BLOCKED: `manage.py migrate` — use `t3 <overlay> worktree provision` instead.",
    ),
    (
        re.compile(r"\bnx\s+serve\b"),
        "BLOCKED: `nx serve` — use `t3 <overlay> worktree start` instead.",
    ),
    (
        re.compile(r"\bdocker\s+compose\s+(?:up|start)\b"),
        "BLOCKED: `docker compose up/start` — use `t3 <overlay> worktree start` instead.",
    ),
    (
        re.compile(r"\b(?:createdb|dropdb)\b"),
        "BLOCKED: `createdb`/`dropdb` — use `t3 <overlay> db reset` instead.",
    ),
    (
        re.compile(r"\b(?:npx\s+)?playwright\s+test\b"),
        "BLOCKED: `playwright test` — use `t3 <overlay> e2e` instead.",
    ),
    (
        re.compile(r"\bnpm\s+run\b"),
        (
            "BLOCKED: `npm run` — use `t3 <overlay> run build-frontend` "
            "(rebuild dist) or `t3 <overlay> worktree start` (full stack) instead."
        ),
    ),
    (
        re.compile(r"\b(?:pipenv|pip)\s+install\b"),
        "BLOCKED: `pip/pipenv install` — use `t3 <overlay> worktree provision` instead.",
    ),
    (
        re.compile(r"\b(?:pg_restore|pg_dump)\b"),
        "BLOCKED: `pg_restore`/`pg_dump` — use `t3 <overlay> db refresh` instead.",
    ),
    (
        re.compile(r"\bdslr\s+(?:restore|import|snapshot|rename|export)\b"),
        (
            "BLOCKED: mutating `dslr` subcommand — use "
            "`t3 <overlay> db refresh --dslr-snapshot <name>` instead. "
            "Only `dslr list` and `dslr delete` are allowed."
        ),
    ),
    (
        re.compile(r"\bgit\s+\S+.*--no-verify\b"),
        "BLOCKED: `--no-verify` — fix the hook failure instead of bypassing it.",
    ),
    (
        re.compile(r"\bgit\s+\S+.*--no-gpg-sign\b"),
        "BLOCKED: `--no-gpg-sign` — do not bypass signing without explicit user approval.",
    ),
    # NOTE: ``gh pr merge`` / ``glab mr merge`` are NOT static-blocked here.
    # A pure regex cannot tell a teatree-managed repo (must use the keystone
    # `t3 <overlay> ticket merge` transition) from a lightweight repo with no
    # ticket/overlay FSM (which had no way to merge at all — a permanent
    # lockout). The cwd-aware ``handle_block_out_of_band_merge`` gate enforces
    # this with a managed-repo carve-out instead (#126).
    (
        re.compile(r"\bsafety\s+(?:check|scan)\b"),
        "BLOCKED: `safety` — use `pip-audit` instead (#1264; `uv audit` is preview-only).",
    ),
    (
        re.compile(r"\buv\s+run\s+(?:\S+\s+)*?t3(?:\s|$)"),
        (
            "BLOCKED: `uv run t3` — teatree is installed globally; call `t3` directly. "
            "If `t3` is missing on this machine, install teatree "
            "(`uv tool install --from git+https://github.com/souliane/teatree.git teatree` "
            "or `uv tool install --editable <teatree-repo>`)."
        ),
    ),
]

# Keep the combined list for any existing code that references _BLOCKED_COMMANDS
# directly (e.g. downstream tests that import it). Both partitions are included
# so the union is identical to the original list.
_BLOCKED_COMMANDS: list[tuple[re.Pattern[str], str]] = [
    *_RAW_SCAN_BLOCKED,
    *_QUOTE_STRIPPED_BLOCKED,
]


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
# ``_session_has_loop`` to gate the loop-registration directive/deny; an active
# long-lived session that never changes its crons keeps an unmodified ``.crons``
# that ages past the retention window. Sweeping it would make
# ``_session_has_loop`` return False and re-emit the loop-registration nag for a
# session that is already running the loop. ``.teatree-active`` is the same
# class: it is touched by ``handle_track_skill_usage`` when a teatree-activating
# skill loads — in a normal session that happens at the start and is not
# repeated for the life of the session — and ``statusline.sh`` gates the WHOLE
# statusline on its presence (exits blank when absent). Sweeping it makes a
# long-lived session's statusline silently go blank. The throttle-and-recreate
# markers (``loop-pending`` / ``pump-armed`` / ``mr_refreshed`` …) are NOT
# listed: their absence is the safe default and they are re-armed on demand.
_SWEEP_PROTECTED_SUFFIXES = frozenset({"crons", "teatree-active"})


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


def emit_pretooluse_deny(reason: str) -> bool:
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
    return _write_pretooluse_deny(decision.reason)


def _write_pretooluse_deny(reason: str) -> bool:
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
    json.dump(payload, sys.stdout)
    return True


# ── Repeated-denial circuit breaker (stop runaway loops burning tokens) ──
#
# A stuck session can hit the SAME gate denial over and over: a real session
# hit one skill-loading denial 16 times consecutively across ~683 model turns,
# burning ~2M output / ~190M total tokens (cache re-reads dominate a runaway
# loop). The model cannot satisfy a false/unsatisfiable demand by retrying, so
# it retries forever. This breaker trips at the K-th CONSECUTIVE identical
# denial and breaks the loop, tiered by gate class:
#
# * UX / non-safety gates (allow-list — the skill-loading gate id at minimum):
#   FAIL OPEN this one call so the loop can make progress, on the theory that K
#   identical UX denials means the demand is false or unsatisfiable. The streak
#   is reset so the next genuine denial starts a fresh count.
# * SAFETY gates (everything NOT on the allow-list — merge/substrate,
#   banned-terms, privacy/leak, out-of-band-merge, orchestrator-boundary): NEVER
#   auto-relax. Keep denying, but escalate the reason so the model stops
#   retrying and uses the documented self-rescue / escalates to the user.
#
# State: a per-session ``<session>.deny-streak`` JSON file holding the current
# denial fingerprint and its consecutive count, in the same STATE_DIR pattern
# as ``.pending`` / ``.skills``. A genuine ALLOW (a PreToolUse call that ran the
# whole chain without a deny) resets the streak in ``main`` so only CONSECUTIVE
# identical denials accumulate. The circuit-broken event is recorded as a
# durable ``loop_circuit_broken`` signal through the same per-session state-file
# seam the SubagentStop no-commit signal uses (``<session>.circuit-broken``); a
# loud one-line stderr warning is the live channel.
#
# Everything is wrapped so the breaker is crash-proof and fast: on ANY internal
# error it falls back to the gate's ORIGINAL decision (deny the original
# reason) — a breaker bug must never itself block a call nor wrongly allow one.

_DENY_STREAK_SUFFIX = "deny-streak"
_CIRCUIT_BROKEN_SUFFIX = "circuit-broken"
_DENY_CIRCUIT_BREAKER_DEFAULT_THRESHOLD = 3

# Reason-prefix markers identifying UX / non-safety gates that MAY auto-relax
# when looped. Conservative allow-list: a deny whose reason does not start with
# one of these is treated as a SAFETY gate and NEVER auto-opens. The
# skill-loading gate is the documented minimum.
_DENY_CIRCUIT_UX_GATE_PREFIXES: tuple[str, ...] = ("SKILL LOADING ENFORCEMENT", "LOOP REGISTRATION")

# Volatile substrings stripped from a deny reason before fingerprinting so "the
# same denial" matches across retries even when the reason embeds a changing
# SHA / path / count. Skill-name lists and gate identity are preserved (they ARE
# the denial's identity), so two DIFFERENT denials still fingerprint apart.
_DENY_FP_VOLATILE_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b[0-9a-f]{7,40}\b", re.IGNORECASE),  # git SHAs
    re.compile(r"(?:/[^\s/]+)+/?"),  # absolute/relative paths
    re.compile(r"\b\d+\b"),  # bare counts/line numbers
)


@dataclasses.dataclass(frozen=True)
class _BreakerDecision:
    """Outcome of the circuit breaker for one deny.

    ``allow`` True means SUPPRESS the deny (auto-relax this call). ``reason`` is
    the (possibly escalation-augmented) reason to emit when ``allow`` is False.
    """

    allow: bool
    reason: str


def _teatree_bool_setting(name: str, *, default: bool = True) -> bool:
    """Best-effort read of a ``[teatree] <name>`` boolean flag from ``~/.teatree.toml``.

    The single shared shape behind every ``[teatree] <flag>_enabled`` reader:
    fails to ``default`` on a missing/broken config, a missing ``[teatree]``
    table, a missing key, or a non-boolean value, and returns the configured
    value only when it is a bare TOML boolean. So only a bare boolean ``false``
    disables a ``default=True`` flag and only a bare boolean ``true`` enables a
    ``default=False`` one — a QUOTED ``"false"`` / ``"true"`` (a string, not a
    bool) is ignored and the default stands. An explicit bare boolean is the
    one-line kill-switch / opt-in, never a code edit (NEVER-LOCKOUT).
    """
    import tomllib  # noqa: PLC0415

    config_path = Path.home() / ".teatree.toml"
    if not config_path.is_file():
        return default
    try:
        with config_path.open("rb") as f:
            config = tomllib.load(f)
    except Exception:  # noqa: BLE001
        return default
    teatree = config.get("teatree") if isinstance(config, dict) else None
    if not isinstance(teatree, dict):
        return default
    value = teatree.get(name)
    return value if isinstance(value, bool) else default


def _deny_circuit_breaker_enabled() -> bool:
    """Whether the repeated-denial circuit breaker is enabled (default True).

    Fails OPEN to enabled on a missing/broken config so the breaker keeps its
    protective default; an explicit ``false`` is the one-line kill-switch that
    makes the breaker a pure pass-through (never a code edit). See
    :func:`_teatree_bool_setting` for the shared bare-boolean semantics.
    """
    return _teatree_bool_setting("deny_circuit_breaker_enabled", default=True)


def _deny_circuit_breaker_threshold() -> int:
    """Consecutive-denial count K at which the breaker trips (default 3).

    Best-effort read of ``[teatree] deny_circuit_breaker_threshold`` from
    ``~/.teatree.toml``. Fails to the default on a missing/broken config or a
    non-positive / non-int value so a malformed config can never disable the
    breaker by setting an impossible threshold.
    """
    import tomllib  # noqa: PLC0415

    config_path = Path.home() / ".teatree.toml"
    if not config_path.is_file():
        return _DENY_CIRCUIT_BREAKER_DEFAULT_THRESHOLD
    try:
        with config_path.open("rb") as f:
            config = tomllib.load(f)
    except Exception:  # noqa: BLE001
        return _DENY_CIRCUIT_BREAKER_DEFAULT_THRESHOLD
    teatree = config.get("teatree") if isinstance(config, dict) else None
    if not isinstance(teatree, dict):
        return _DENY_CIRCUIT_BREAKER_DEFAULT_THRESHOLD
    value = teatree.get("deny_circuit_breaker_threshold")
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return _DENY_CIRCUIT_BREAKER_DEFAULT_THRESHOLD
    return value


def _deny_gate_id(reason: str) -> str:
    """Stable gate identity derived from the deny reason's leading marker.

    A reason like ``SKILL LOADING ENFORCEMENT: …`` or ``BLOCKED: `nx serve` …``
    carries its gate identity in a leading marker that is constant across
    retries (the variable tail — the offending skill list / command — is part
    of the FINGERPRINT, not the gate id). The id is the marker up to the first
    ``:`` (or the first few words when there is none), normalised to a compact
    token so the same gate maps to the same id.
    """
    head = reason.split(":", 1)[0] if ":" in reason else " ".join(reason.split()[:4])
    return re.sub(r"\s+", "-", head.strip().lower())[:64] or "unknown-gate"


def _deny_is_ux_gate(reason: str) -> bool:
    """True iff *reason* belongs to an allow-listed UX / non-safety gate.

    Conservative: anything not matching an allow-list prefix is a SAFETY gate
    and is never auto-relaxed by the breaker.
    """
    stripped = reason.lstrip()
    return any(stripped.startswith(prefix) for prefix in _DENY_CIRCUIT_UX_GATE_PREFIXES)


def _deny_fingerprint(gate_id: str, reason: str) -> str:
    """Stable short hash of gate identity + a volatility-normalised reason.

    Volatile substrings (SHAs, paths, bare counts) are stripped so the same
    logical denial fingerprints identically across retries, while a genuinely
    different denial (different gate, different unloaded-skill set) fingerprints
    apart. Returns a short hex digest; never raises.
    """
    normalised = reason
    for pattern in _DENY_FP_VOLATILE_RES:
        normalised = pattern.sub(" ", normalised)
    normalised = re.sub(r"\s+", " ", normalised).strip().lower()
    digest = hashlib.sha256(f"{gate_id}\x00{normalised}".encode()).hexdigest()
    return digest[:16]


def _bump_deny_streak(session_id: str, fingerprint: str) -> int:
    """Increment the consecutive-denial count for *fingerprint*; return the new count.

    If the stored fingerprint matches, the count increments; otherwise the
    streak resets to 1 for the new fingerprint. Crash-proof: any IO/JSON error
    is swallowed and the call is counted as 1 (a single isolated denial), so a
    state-file fault can never manufacture a trip nor crash the gate.
    """
    if not session_id:
        return 1
    path = _state_file(session_id, _DENY_STREAK_SUFFIX)
    try:
        _ensure_state_dir()
        count = 0
        if path.is_file():
            stored = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(stored, dict) and stored.get("fp") == fingerprint:
                raw = stored.get("count", 0)
                count = raw if isinstance(raw, int) and not isinstance(raw, bool) else 0
        new_count = count + 1
        path.write_text(json.dumps({"fp": fingerprint, "count": new_count}), encoding="utf-8")
    except (OSError, ValueError, TypeError):
        return 1
    return new_count


def _reset_deny_streak(session_id: str) -> None:
    """Clear the per-session deny-streak so only CONSECUTIVE denials accumulate.

    Called on every ALLOWED PreToolUse call (genuine progress) and after the
    breaker relaxes a UX gate. Best-effort — a failure to clear is harmless (the
    next bump with a new fingerprint resets the count anyway).
    """
    if not session_id:
        return
    with contextlib.suppress(OSError):
        _state_file(session_id, _DENY_STREAK_SUFFIX).unlink(missing_ok=True)


def _record_circuit_broken_signal(session_id: str, gate_id: str, fingerprint: str, count: int) -> None:
    r"""Persist + log one ``loop_circuit_broken`` signal (deduped by fingerprint).

    Durable channel: append a deduped ``<gate_id>\t<fingerprint>\t<count>`` line
    to the per-session ``<session>.circuit-broken`` state file — the same seam
    the SubagentStop no-commit signal uses (``<session>.no-commit``), which the
    PreCompact recovery snapshot already knows how to read back. Best-effort: a
    record failure must never propagate out of the deny path.
    """
    if not session_id:
        return
    with contextlib.suppress(OSError):
        _ensure_state_dir()
        path = _state_file(session_id, _CIRCUIT_BROKEN_SUFFIX)
        for line in _read_lines(path):
            if line.split("\t", 1)[0] == fingerprint:
                return
        _append_line(path, f"{fingerprint}\t{gate_id}\t{count}")


def _apply_deny_circuit_breaker(reason: str) -> _BreakerDecision:
    """Route one PreToolUse deny through the repeated-denial circuit breaker.

    Returns a :class:`_BreakerDecision`: ``allow=True`` means SUPPRESS the deny
    (a looped UX gate auto-relaxed this call); otherwise ``reason`` is the deny
    reason to emit (escalation-augmented for a looped safety gate).

    Crash-proof: on a disabled breaker, a non-PreToolUse invocation, or ANY
    internal error, the original deny is preserved unchanged (fall back to the
    gate's original decision — the breaker never blocks nor wrongly allows on
    its own fault).
    """
    try:
        if _CURRENT_EVENT != "PreToolUse" or not _deny_circuit_breaker_enabled():
            return _BreakerDecision(allow=False, reason=reason)
        session_id = _CURRENT_DATA.get("session_id", "") if isinstance(_CURRENT_DATA, dict) else ""
        threshold = _deny_circuit_breaker_threshold()
        gate_id = _deny_gate_id(reason)
        fingerprint = _deny_fingerprint(gate_id, reason)
        count = _bump_deny_streak(session_id, fingerprint)
        if count < threshold:
            return _BreakerDecision(allow=False, reason=reason)

        _record_circuit_broken_signal(session_id, gate_id, fingerprint, count)
        if _deny_is_ux_gate(reason):
            sys.stderr.write(
                f"CIRCUIT BREAKER: gate '{gate_id}' denied {count} times consecutively "
                "— auto-relaxing this call to break the loop; root cause is likely a "
                "false or unsatisfiable demand. Investigate the gate, do not just retry.\n"
            )
            _reset_deny_streak(session_id)
            return _BreakerDecision(allow=True, reason=reason)

        sys.stderr.write(
            f"CIRCUIT BREAKER: safety gate '{gate_id}' denied {count} times consecutively "
            "— NOT auto-relaxing a safety gate; escalating to break the loop.\n"
        )
        escalation = (
            f"\n\nCIRCUIT BREAKER: this identical call has been denied {count} times — "
            "you are LOOPING. STOP retrying; retrying only burns tokens and changes "
            "nothing. Use the documented self-rescue / escape, or escalate to the user."
        )
        return _BreakerDecision(allow=False, reason=f"{reason}{escalation}")
    except Exception:  # noqa: BLE001 — breaker failure falls back to the gate's original deny.
        return _BreakerDecision(allow=False, reason=reason)


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
        from teatree.cli import teatree_gate  # noqa: PLC0415
        from teatree.hooks import self_rescue  # noqa: PLC0415
    except Exception:  # noqa: BLE001
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
    except Exception:  # noqa: BLE001
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
    except Exception:  # noqa: BLE001
        return False


def _fail_open_or_deny(data: dict, reason: str) -> bool:
    """Deny with ``reason`` unless a self-rescue command or fail-open says allow.

    The single chokepoint every OVER-DENY gate routes its deny through. A
    self-rescue command is always allowed; an enabled master fail-open switch
    allows everything; otherwise the deny is emitted. Returns ``True`` (deny
    emitted) or ``False`` (allow), so callers ``return _fail_open_or_deny(...)``.

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
        return emit_pretooluse_deny(reason)
    return emit_pretooluse_deny(reason)


def _state_file(session_id: str, suffix: str) -> Path:
    return STATE_DIR / f"{session_id}.{suffix}"


def _teatree_active(session_id: str) -> bool:
    if not session_id:
        return False
    return _state_file(session_id, "teatree-active").is_file()


def _loop_auto_load_active(session_id: str) -> bool:
    """Whether this session may auto-arm the loop/statusline machinery (#256).

    The single gate every session-start auto-load injection point shares —
    the loop-registration nudge (:func:`handle_enforce_loop_on_prompt`,
    :func:`_loop_registration_exempt`) and the tick-owner bootstrap
    (:func:`handle_session_start_bootstrap`). Two conditions must BOTH hold:

    - the session opted into teatree (:func:`_teatree_active` — a teatree
        skill was loaded), AND
    - the operator explicitly enabled auto-load (:func:`_loops_auto_load_enabled`).

    The second condition defaults OFF so a colleague who merely clones the
    repo (and even loads a teatree skill) is never nagged to register a cron
    or shown the loop statusline. The loop owner opts in once via
    ``[loops] auto_load = true`` in ``~/.teatree.toml`` (or
    ``T3_LOOPS_AUTO_LOAD=1``) and keeps the existing behaviour intact.
    """
    return _teatree_active(session_id) and _loops_auto_load_enabled()


def _is_teatree_skill(name: str) -> bool:
    normalized = normalize_skill_name(name)
    return normalized in {"t3:teatree", "teatree"}


def _bare_skill_segment(name: str) -> str:
    """The trigger index's key form: the bare segment after a namespace prefix.

    ``build_trigger_index`` keys every entry (and its ``requires:`` members)
    by the bare skill-directory name, so a qualified Skill-tool token like
    ``t3:teatree-dogfood`` must be mapped DOWN to ``teatree-dogfood`` to match
    an index entry and resolve its ``requires:`` closure.
    """
    return name.rstrip("/").removesuffix("/SKILL.md").rsplit("/", 1)[-1].rsplit(":", 1)[-1]


def _skill_load_activates_teatree(skills: list[str]) -> bool:
    """Does loading *skills* opt the session into teatree (directly or via requires:)?

    Resolves the ``requires:`` closure against a bare-mapped copy of the input
    so a qualified Skill-tool token (``t3:teatree-dogfood``) expands the same as
    its bare InstructionsLoaded spelling — the trigger index is bare-keyed. The
    bare mapping is scoped to this detection only; the recorded ``.skills``
    closure keeps its own resolution + canonicalization contract.
    """
    bare = [_bare_skill_segment(s) for s in skills]
    return any(_is_teatree_skill(s) for s in _resolve_skill_closure(bare))


_read_lines = read_lines
_append_line = append_line


# ── UserPromptSubmit ────────────────────────────────────────────────

# Harness-injected ambient context — NOT task intent. The Claude Code
# harness appends ``<system-reminder>…</system-reminder>`` blocks (the
# CLAUDE.md body, the MEMORY.md index, the available-skills listing) to
# the prompt that reaches ``UserPromptSubmit``. Keyword-matching those
# blocks is the #1567 over-fire: a MEMORY.md index line naming
# ``feedback_blog_*`` keyword-matched ``\bblog\b`` → suggested
# ``ac-writing-blog-posts`` → the PreToolUse gate hard-blocked every
# Bash/Edit/Write during an unrelated autonomous loop. The hard-block
# demand set must derive from genuine task-intent text only, so these
# wrappers are stripped before the prompt is matched.
_AMBIENT_CONTEXT_RE = re.compile(
    r"<(system-reminder|command-message|command-name|command-args|local-command-stdout)\b[^>]*>"
    r".*?</\1>",
    re.DOTALL | re.IGNORECASE,
)

# The block regex is O(n²) against many UNTERMINATED open tags (a user
# pasting a large log/transcript that quotes literal ``<system-reminder>``
# open tags, or a malicious agent). ``_strip_ambient_context`` runs on
# EVERY ``UserPromptSubmit`` and is net-new hot-path cost, so the input is
# capped before the regexes run — bounding the worst case well under the
# 5s ``UserPromptSubmit`` timeout (hooks/CLAUDE.md "hooks must be fast").
# Genuine task intent sits early in the prompt (the harness appends ambient
# blocks), so a 64 KiB cap never truncates intent — mirrors the 512-char
# token windows used elsewhere in this file.
_AMBIENT_STRIP_MAX_CHARS: int = 65536


def _strip_ambient_context(prompt: str) -> str:
    """Remove harness-injected ambient-context blocks from *prompt*.

    Returns the prompt with every ``<system-reminder>`` / harness
    ``<command-*>`` wrapper (and its body) removed, leaving only the
    genuine task-intent text. An unterminated opening wrapper (truncated
    injection) is dropped from its tag to end-of-string so leaked ambient
    text can never reach the keyword matcher. The intent text is what the
    high-confidence hard-block demand set is built from (#1567).

    The input is capped to :data:`_AMBIENT_STRIP_MAX_CHARS` before
    matching to keep this hot-path hook fast (see the constant's note).
    """
    prompt = prompt[:_AMBIENT_STRIP_MAX_CHARS]
    stripped = _AMBIENT_CONTEXT_RE.sub(" ", prompt)
    stripped = re.sub(
        r"<(system-reminder|command-message|command-name|command-args|local-command-stdout)\b[^>]*>.*",
        " ",
        stripped,
        flags=re.DOTALL | re.IGNORECASE,
    )
    return stripped.strip()


def _build_skill_loader_input(prompt: str, session_id: str) -> dict:
    teatree_home = os.environ.get("HOME", "")
    source_root = Path(__file__).resolve().parents[2].parent

    active = _read_lines(_state_file(session_id, "active"))
    loaded = _read_lines(_state_file(session_id, "skills"))

    search_dirs = [str(source_root), f"{teatree_home}/.agents/skills", f"{teatree_home}/.claude/skills"]
    return {
        "prompt": _strip_ambient_context(prompt),
        "cwd": str(Path.cwd()),
        "active_repos": active,
        "loaded_skills": loaded,
        "skill_search_dirs": [d for d in search_dirs if d],
        "supplementary_config": os.environ.get("T3_SUPPLEMENTARY_SKILLS", f"{teatree_home}/.teatree-skills.yml"),
    }


def handle_user_prompt_submit(data: dict) -> None:
    """Detect intent and suggest skills via skill_loader.suggest_skills()."""
    session_id = data.get("session_id", "")
    prompt = data.get("prompt", "")
    if not session_id or not prompt:
        return

    _ensure_state_dir()
    pending = _state_file(session_id, "pending")
    pending.write_text("", encoding="utf-8")

    scripts_dir = Path(__file__).resolve().parent.parent.parent / "scripts"
    if not (scripts_dir / "lib" / "skill_loader.py").is_file():
        return

    loader_input = _build_skill_loader_input(prompt, session_id)

    sys.path.insert(0, str(scripts_dir))
    try:
        from lib.skill_loader import suggest_skills  # noqa: PLC0415

        result = suggest_skills(loader_input)
    except Exception:  # noqa: BLE001
        return
    finally:
        sys.path.pop(0)

    suggestions = result.get("suggestions", [])
    advisory = set(result.get("advisory", []))

    # Deterministic t3 CLI reminder — injected when prompt matches
    # workspace/infrastructure patterns, regardless of skill suggestions.
    t3_reminder = _T3_CLI_REMINDER if _T3_CLI_REMINDER_RE.search(prompt) else ""

    if not suggestions:
        if t3_reminder:
            print(t3_reminder)  # noqa: T201
        return

    skill_list = ", ".join(f"/{s}" for s in suggestions)
    # Advisory skills come from the loose supplementary keyword config
    # (~/.teatree-skills.yml), whose bare-token regexes (e.g. \bruff\b)
    # over-fire on incidental mentions (#1683). They are suggested but kept
    # OUT of <session>.pending so the PreToolUse gate never hard-blocks a
    # Bash/Edit/Write on an incidental keyword match. Only intent / framework
    # / overlay / companion skills enforce load-first.
    demanded = [s for s in suggestions if s not in advisory]
    pending.write_text("\n".join(normalize_skill_name(s) for s in demanded) + "\n", encoding="utf-8")
    parts = [f"LOAD THESE SKILLS NOW (call the Skill tool for each, before doing anything else): {skill_list}."]
    if t3_reminder:
        parts.append(t3_reminder)
    print("\n".join(parts))  # noqa: T201


# ── UserPromptSubmit: live-presence heartbeat (#58 away-misclassification) ────


def _is_bare_loop_prompt(prompt: str) -> bool:
    """True when *prompt* is a PURE autonomous loop tick (no user content).

    A cron-fired tick reaches ``UserPromptSubmit`` as the loop prompt plus,
    optionally, the harness-injected ``<system-reminder>`` ambient blocks — both
    strip down to exactly the bare loop prompt. Two bare shapes count: the legacy
    fat-tick ``_LOOP_PROMPT`` and a per-loop tick ``t3 loops tick --loop <name>``
    (#2650, recognised via the seam-synced :func:`is_bare_loop_tick_prompt`). A
    genuine fresh user prompt that the harness delivers PREFIXED by the loop
    continuation text leaves residual user content after the strip, so it is NOT
    bare. The ambient strip reuses :func:`_strip_ambient_context` (the same
    normalisation the skill-load gate applies), keeping one definition of "what
    the harness appends".
    """
    stripped = _strip_ambient_context(prompt)
    return stripped == _LOOP_PROMPT.strip() or is_bare_loop_tick_prompt(stripped)


def handle_record_presence(data: dict) -> None:
    """Stamp a live-presence heartbeat — a prompt proves the user is here.

    ``availability.resolve_mode`` reads this stamp to upgrade a
    schedule-derived ``away`` to ``present``: a user actively submitting
    prompts is demonstrably reachable, so their ``AskUserQuestion`` calls
    must not be deferred just because the clock is outside their configured
    work hours. Fail-open and silent on the happy path — a heartbeat that
    cannot be written never blocks the prompt (the schedule then decides
    as before).
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
    if not bootstrap_teatree_django():
        return
    try:
        from teatree.core.availability import PRESENCE  # noqa: PLC0415

        PRESENCE.record(session_id=str(data.get("session_id", "")))
    except Exception:  # noqa: BLE001 — heartbeat is best-effort; never block the prompt.
        return


# ── UserPromptSubmit + PreToolUse: enforce-loop-registration ──────────

_LOOP_CADENCE_DEFAULT = 720


def _loops_toml_enabled() -> bool:
    """Whether ``[loops] enabled`` is true in ``~/.teatree.toml`` (default True).

    Fails open (True) on a missing or broken config — only an explicit
    ``false`` suppresses loop behavior.
    """
    import tomllib  # noqa: PLC0415

    config_path = Path.home() / ".teatree.toml"
    if not config_path.is_file():
        return True
    try:
        with config_path.open("rb") as f:
            config = tomllib.load(f)
    except Exception:  # noqa: BLE001
        return True
    loops = config.get("loops") if isinstance(config, dict) else None
    if not isinstance(loops, dict):
        return True
    return loops.get("enabled") is not False


_AUTO_LOAD_TRUTHY: frozenset[str] = frozenset({"1", "true", "yes", "on"})


def _loops_auto_load_enabled() -> bool:
    """Whether the operator opted into session-start loop/statusline auto-load (#256).

    The opt-in knob the loop OWNER sets; default OFF so a colleague cloning
    the repo is never nagged. Resolved env-first (``T3_LOOPS_AUTO_LOAD`` via
    :func:`_resolve_loop_env`, so the ``~/.teatree`` bash env file the
    unsourced hook misses is still honoured), then ``[loops] auto_load`` in
    ``~/.teatree.toml``. Unlike :func:`_loops_toml_enabled` (a fail-OPEN
    kill-switch), this fails CLOSED (OFF) on a missing/broken config: a fresh
    clone has neither the env var nor the flag, so it stays silent.
    """
    import tomllib  # noqa: PLC0415

    env = _resolve_loop_env("T3_LOOPS_AUTO_LOAD").strip().lower()
    if env:
        return env in _AUTO_LOAD_TRUTHY

    config_path = Path.home() / ".teatree.toml"
    if not config_path.is_file():
        return False
    try:
        with config_path.open("rb") as f:
            config = tomllib.load(f)
    except Exception:  # noqa: BLE001
        return False
    loops = config.get("loops") if isinstance(config, dict) else None
    if not isinstance(loops, dict):
        return False
    return loops.get("auto_load") is True


_LOOP_PROMPT = "Run `t3 loop tick` in Bash, then briefly report the tick summary."


def _loop_cadence_seconds() -> int:
    """Resolve the loop cadence the same way ``t3 loop`` does (#1036).

    Routes through the shared ``teatree.config.cadence_seconds()`` resolver
    (``T3_LOOP_CADENCE`` env first, then ``~/.teatree.toml``
    ``loop_cadence_seconds``) so the hook's tick-staleness window and the
    loop-registration cron minutes can never diverge from the real slot
    cadence. Best-effort: if ``teatree`` is not importable in this hook
    process, fall back to the env-only read.
    """
    try:
        with _teatree_src_on_path():
            from teatree.config import cadence_seconds  # noqa: PLC0415

            return cadence_seconds()
    except Exception:  # noqa: BLE001
        return int(os.environ.get("T3_LOOP_CADENCE", _LOOP_CADENCE_DEFAULT) or _LOOP_CADENCE_DEFAULT)


def _tick_meta_stale() -> bool:
    xdg = os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))
    meta = Path(xdg) / "teatree" / "tick-meta.json"
    if not meta.is_file():
        return True
    cadence = _loop_cadence_seconds()
    import time  # noqa: PLC0415

    age = int(time.time()) - int(meta.stat().st_mtime)
    return age > cadence * 2


def _session_has_loop(session_id: str) -> bool:
    crons_file = _state_file(session_id, "crons")
    if not crons_file.is_file():
        return False
    try:
        data = json.loads(crons_file.read_text(encoding="utf-8"))
        return bool(data.get("jobs"))
    except (json.JSONDecodeError, OSError):
        return False


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
    No-ops if a live foreign owner already holds the record, or if any of
    the loop kill-switches are engaged: ``[loops] enabled = false`` in
    ``~/.teatree.toml``, ``T3_LOOPS_DISABLED=all``, or ``T3_LOOP_DISOWN``
    truthy.  Re-arming a paused loop here would resurrect the very
    machinery the pause surface exists to silence.
    """
    if not _loops_toml_enabled():
        return
    if _all_loops_disabled():
        return
    if _resolve_loop_env("T3_LOOP_DISOWN").strip() not in _DISOWN_FALSEY:
        return
    current_pid = os.getppid()
    with _loop_registry_txn() as box:
        registry = _prune_dead_owner(box[0])
        owner = registry.get(_OWNER_LOOP)
        if owner is not None and owner.get("session_id") != session_id:
            box[0] = registry
            return
        if owner is None:
            db_live = _db_live_foreign_owner(session_id, current_pid=current_pid)
            if db_live:
                box[0] = registry
                return
        box[0] = _tick_owner_record(session_id, "")


def handle_enforce_loop_on_prompt(data: dict) -> None:
    """On first prompt, the loop OWNER registers one ``/loop`` per enabled DB Loop (#2650).

    One ``/loop`` per ENABLED ``Loop`` row, each on its own cadence.  Only the
    owner session (``_loop_auto_load_active`` + ``_claim_loop_ownership``) registers.
    Directive building lives in the bare sibling :mod:`loop_registrations`.
    Fail-open: zero enabled loops emits nothing, so the PreToolUse nudge never
    fires when there is nothing to register.

    ``_session_has_loop`` is the sole registration gate.  A fresh
    ``tick-meta.json`` from a prior session (e.g. after release + claim) must
    NOT suppress registration — that was the #2714 stall bug.
    """
    session_id = data.get("session_id", "")
    if not session_id:
        return
    if not _loop_auto_load_active(session_id):
        return
    _claim_loop_ownership(session_id)
    _ensure_state_dir()
    _cleanup_stale_pending(session_id)
    pending = _state_file(session_id, "loop-pending")
    if _session_has_loop(session_id):
        pending.unlink(missing_ok=True)
        return
    if emit_loop_registrations(sys.stdout):
        pending.write_text("1", encoding="utf-8")


def _loop_registration_gate_enabled() -> bool:
    """Whether the loop-registration PreToolUse gate is enabled (default True).

    Fails OPEN to enabled on a missing/broken config; an explicit ``false`` is
    the one-line durable kill-switch — never a code edit (NEVER-LOCKOUT). See
    :func:`_teatree_bool_setting` for the shared bare-boolean semantics.
    """
    return _teatree_bool_setting("loop_registration_gate_enabled", default=True)


_LOOP_REGISTRATION_EXEMPT_TOOLS = frozenset(
    {"CronCreate", "CronDelete", "CronList", "ScheduleWakeup", "Skill", "ToolSearch"}
)


def _loop_registration_exempt(data: dict) -> bool:
    """True when this call must NOT be nudge-blocked for loop registration.

    Groups the side-effect-free NEVER-LOCKOUT exemptions so the handler stays a
    single decision. A call is exempt when any of these holds:

    - the session has not opted into session-start loop auto-load
        (``_loop_auto_load_active`` False — no teatree marker OR auto-load not
        enabled, #256), so a colleague who merely cloned the repo is never
        nagged to register a cron; default OFF until the owner opts in via
        ``[loops] auto_load = true``;
    - the tool is a cron-management / skill tool the agent uses to register the
        loop (no point blocking the very tools that satisfy the gate);
    - the call comes from a sub-agent (non-empty ``agent_id``) — a sub-agent has
        no ``CronCreate`` tool, so a deny is an *unrecoverable* lockout that
        killed every spawned coder/reviewer in the incident;
    - the durable kill-switch ``[teatree] loop_registration_gate_enabled =
        false`` is set (disable without a code edit);
    - there is no ``session_id`` (no per-session marker to key on);
    - this session is NOT the loop driver — a *different* live session already
        owns the tick (``_session_drives_loop`` is False), so this is an
        attended, non-owner interactive session. Nagging it to ``CronCreate`` a
        competing ``t3 loop tick`` would only spawn a duplicate loop the
        non-owner tick gate would SKIP anyway; the rightful owner (or, with no
        live owner, the next eligible session — see ``_session_drives_loop``)
        still gets nagged, so the loop is never left unregistered.
    """
    if not _loop_auto_load_active(data.get("session_id", "")):
        return True
    if data.get("tool_name", "") in _LOOP_REGISTRATION_EXEMPT_TOOLS:
        return True
    if _call_is_from_subagent(data):
        return True
    if not _loop_registration_gate_enabled():
        return True
    if not data.get("session_id"):
        return True
    return not _session_drives_loop(data["session_id"])


def handle_enforce_loop_registration(data: dict) -> bool:
    """Nudge-block Bash/Edit/Write until the background loop cron is registered.

    NEVER-LOCKOUT: this is the loop-bootstrap NUDGE, not a safety gate, so it
    must never be able to wedge a session (it hard-locked the factory several
    times — the worst recurring incident). The exemptions in
    :func:`_loop_registration_exempt` (cron tools, sub-agents, kill-switch,
    no-session) cover the first two layers; the deny itself adds two more:

    - it routes through :func:`_fail_open_or_deny`, so the always-allowed
        self-rescue commands and the master ``danger_gate_fail_open`` switch relax it;
    - the reason carries the ``LOOP REGISTRATION`` UX-gate prefix, so the
        repeated-denial circuit breaker auto-relaxes it after K consecutive
        denials instead of blocking forever.
    """
    if _loop_registration_exempt(data):
        return False
    session_id = data["session_id"]
    pending = _state_file(session_id, "loop-pending")
    if not pending.is_file():
        return False
    if _session_has_loop(session_id):
        pending.unlink(missing_ok=True)
        return False
    reason = (
        "LOOP REGISTRATION: the teatree background loops are not registered yet. "
        "Register one native Claude `/loop` per enabled loop — see the session-start "
        "registration directive, or run `t3 loops list`, then `t3 loop claude-spec <name>` "
        "and CronCreate each. To run without the loops, set "
        "[teatree] loop_registration_gate_enabled = false."
    )
    return _fail_open_or_deny(data, reason)


# ── UserPromptSubmit: todo-freshness nudge ──────────────────────────

_TODO_FRESHNESS_NUDGE = (
    "Session housekeeping: keep the task/TODO list current. "
    "Reflect finished work as completed and surface any newly discovered work "
    "as its own task before continuing."
)


def handle_todo_freshness_nudge(data: dict) -> None:
    """Once per session, nudge keeping the task/TODO list current.

    Ordinary per-session housekeeping — fires in-session, never as a sub-agent
    and unrelated to the monitor/work-trigger loop. Idempotent via a
    per-session ``<session>.todo-nudged`` marker, mirroring the loop-pending
    precedent. Advisory only: prints additionalContext, never emits a deny,
    so it can never block tool use.
    """
    session_id = data.get("session_id", "")
    if not session_id:
        return
    _ensure_state_dir()
    marker = _state_file(session_id, "todo-nudged")
    if marker.exists():
        return
    marker.write_text("1", encoding="utf-8")
    print(_TODO_FRESHNESS_NUDGE)  # noqa: T201


# ── PreToolUse: enforce-skill-loading ───────────────────────────────
#
# The gate blocks Bash/Edit/Write until every suggested-but-unloaded
# skill is loaded. A suggestion lands in ``<session>.pending`` from the
# supplementary keyword config (``~/.teatree-skills.yml``) or from
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
# This is the INVERSE operation from RESOLUTION (:func:`_skill_resolves`),
# which deliberately does NOT touch the namespace — see its docstring.


def _skill_resolves(name: str, search_dirs: list[Path]) -> bool:
    """True iff *name* resolves to a loadable skill in *search_dirs*.

    Resolution is deliberately CONSERVATIVE: a name resolves only when its
    own skill directory exists VERBATIM. Two shapes reach
    ``<session>.pending``. A bare name (lifecycle ``code``, supplementary
    ``ac-*``) matches ``<dir>/<name>/SKILL.md``. An overlay ``skill_path``
    (``skills/<skill>/SKILL.md``, emitted by the overlay generator) matches
    when the literal path is a file under a search dir (or its parent), or
    when its ``<skill>`` parent-dir name exists as a skill dir.

    No namespace ``:``-stripping is performed in either branch — stripping
    would mis-resolve a stale ``old:code`` / ``skills/old:code/SKILL.md``
    onto an installed bare ``code`` and re-introduce the very fail-closed
    lockout class this gate exists to prevent. A name that resolves only by
    discarding its namespace is treated as unresolvable (fail open).

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
        segment = stripped.rsplit("/", 1)[-1]
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


# Per-call escape mirroring the ``[skip-skill-gate: <reason>]`` token of
# the sibling TaskCreated gate and the ``[fg-ok: <reason>]`` precedent of
# the orchestrator-boundary gate: ``[skill-load-ok: <non-empty-reason>]``
# in the CURRENT tool call's command/args unblocks this single Bash/Edit/
# Write, an empty reason rejects. A false skill-trigger can therefore
# never wedge the loop — but a genuine intent match still hard-blocks
# every call that does NOT carry the escape (the #1488 loophole stays
# closed).
_SKILL_LOAD_OK_RE = re.compile(r"\[skill-load-ok:\s*(\S[^\]]*?)\s*\]")

# Per-call escape for the plan-edit gate: ``[skip-plan-gate: <non-empty-reason>]``
# in the current Edit/Write tool call's new_string/content/file_path unblocks that
# single call. Mirrors ``_SKILL_LOAD_OK_RE`` / ``_SKIP_SKILL_GATE_RE`` in shape
# and 512-char truncation scope — buried tokens do not silently escape.
_SKIP_PLAN_GATE_RE = re.compile(r"\[skip-plan-gate:\s*(\S[^\]]*?)\s*\]")


def _skill_load_ok_token(data: dict) -> str | None:
    """Return the reason from a ``[skill-load-ok: <reason>]`` token, else None.

    Scans the current tool call's command/args — for ``Bash`` the
    ``command`` string, for ``Edit``/``Write`` the written text
    (``new_string`` / ``content``) and the ``file_path`` — within the
    first 512 characters of each field (matching
    :func:`_task_text_skip_token`) so a buried token in a long body does
    not silently authorise the call. An empty reason returns None.
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


def _skip_plan_gate_token(data: dict) -> str | None:
    """Return the reason from a ``[skip-plan-gate: <reason>]`` token, else None.

    Scans the current Edit/Write tool call's ``new_string``, ``content``,
    and ``file_path`` within the first 512 characters of each field —
    mirroring :func:`_skill_load_ok_token` — so a buried token in a long
    body does not silently authorise the call. An empty reason returns None.
    """
    tool_input = data.get("tool_input", {})
    if not isinstance(tool_input, dict):
        return None
    for field in ("new_string", "content", "file_path"):
        value = tool_input.get(field, "")
        if not isinstance(value, str) or not value:
            continue
        match = _SKIP_PLAN_GATE_RE.search(value[:512])
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

    NEVER-LOCKOUT (#1918): a loop-registration / loop-owner bootstrap turn
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
    lacking that token.
    """
    session_id = data.get("session_id", "")
    if not session_id or not _skill_gate_targets_code_work(data) or _skill_loading_exempt(session_id):
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


# ── TaskCreated: enforce-skill-loading-on-task-create (#1488) ─────────
#
# ``ultracode`` (and any harness Workflow/Task fan-out) spawns sub-agents
# through the Task/Workflow vehicle, which BYPASSES ``PreToolUse`` hooks
# (a known regression from TodoWrite — see ``docs/claude-code-internals.md``
# §9). The ``PreToolUse`` skill-loading gate above
# (:func:`handle_enforce_skill_loading`, matcher ``Bash|Edit|Write``) is
# therefore never consulted on the fan-out, so sub-agents skip
# auto-loading the matching teatree lifecycle skill. That is the loophole
# that let a bespoke review workflow run instead of ``/t3:review``.
#
# The teatree skill injection reaches the MAIN agent only. The fanned-out
# sub-agent starts BLANK: it holds only its task prompt and lacks the
# ``Skill`` tool, so what the PARENT session loaded does NOT transfer to it.
# The gate is therefore satisfied by the DISPATCH PROMPT instructing the
# sub-agent to load the skill — not by the parent's loaded set. The whole
# demand computation + never-lockout fail-open lives in the
# ``subagent_skill_gate`` sibling behind ``unreferenced_demand_reason`` (over
# ``required_skills_for_task`` / ``filter_unreferenced`` /
# ``build_load_first_reason``); the router only calls that one entry point.
#
# The ``TaskCreated`` event DOES fire for the fan-out vehicle (verified
# against the Claude Code 2.1.156 binary: ``hook_event_name:"TaskCreated"``
# with ``task_id``/``task_subject``/``task_description``; a hook output of
# ``{"continue": false, ...}`` sets ``preventContinuation``).
#
# It enforces SKILL-LOADING ONLY — it never inspects agent count, token
# budget, ``run_in_background``, or any workflow-size field, so ultracode
# keeps maximal fan-out room. The deny schema is the teammate-stop
# envelope (``{"continue": false, "stopReason": ...}``), NOT the
# ``PreToolUse`` ``hookSpecificOutput`` deny; ``main`` translates the
# handler's ``True`` return into ``sys.exit(2)`` the same as the
# ``PreToolUse`` gates.

# ``[skip-skill-gate: <non-empty-reason>]`` anywhere in the subject/description
# head unblocks the dispatch; an empty reason rejects.
_SKIP_SKILL_GATE_RE = re.compile(r"\[skip-skill-gate:\s*(\S[^\]]*?)\s*\]")


def _skill_loading_gate_enabled() -> bool:
    """Whether the skill-loading-on-task-create gate is enabled (default True).

    Fails OPEN to enabled on a missing/broken config so the gate keeps its
    protective default; an explicit ``false`` is the one-line kill-switch
    (never a code edit). See :func:`_teatree_bool_setting` for the shared
    bare-boolean semantics.
    """
    return _teatree_bool_setting("skill_loading_gate_enabled", default=True)


def _task_text_skip_token(text: str) -> str | None:
    """Return the reason from a ``[skip-skill-gate: <reason>]`` token, else None.

    Scans only the first 512 characters (matching
    :func:`_agent_prompt_skip_token`) so a buried token in a long task body
    does not silently authorise dispatch.
    """
    match = _SKIP_SKILL_GATE_RE.search(text[:512])
    if not match:
        return None
    return match.group(1).strip() or None


def emit_task_create_deny(reason: str) -> bool:
    """Emit the ``TaskCreated`` deny envelope and return ``True``.

    The harness blocks task creation when a hook emits ``continue: false``
    (it sets ``preventContinuation``) — a DIFFERENT schema from the
    ``PreToolUse`` ``hookSpecificOutput`` deny. ``main`` translates the
    ``True`` return into ``sys.exit(2)``, the documented block signal for
    the ``TaskCreated``/``TaskCompleted`` events.
    """
    json.dump({"continue": False, "stopReason": reason}, sys.stdout)
    return True


def handle_enforce_skill_loading_on_task_create(data: dict) -> bool:
    """Demand the fanned-out task's DISPATCH PROMPT instruct skill-loading.

    A sub-agent spawned via the Workflow/Task fan-out starts BLANK — it holds
    only its task prompt and lacks the ``Skill`` tool, so what the PARENT
    session loaded does NOT transfer to it. The gate is therefore satisfied by
    the dispatch PROMPT referencing the skill (a ``/t3:<name>`` token, a
    ``<name>/SKILL.md`` path, or a ``Skill tool`` / ``load the <name> skill``
    instruction), NOT by the parent's loaded set.

    The deny reason (un-derivable ROOTS + ``<session>.pending``, minus
    resolvable-and-already-referenced) and its never-lockout fail-open are owned
    by :func:`unreferenced_demand_reason`. Skill-loading ONLY: no agent-count /
    budget / size field is read, so ultracode keeps maximal fan-out room. Fails
    open on the kill-switch, a valid ``[skip-skill-gate: <reason>]`` token, or a
    missing session id.
    """
    session_id = data.get("session_id", "")
    if not session_id or not _skill_loading_gate_enabled():
        return False

    subject = data.get("task_subject", "") or ""
    description = data.get("task_description", "") or ""
    prompt = f"{subject}\n{description}"
    if _task_text_skip_token(prompt):
        return False

    search_dirs = _skill_search_dirs()
    reason = unreferenced_demand_reason(
        prompt=prompt,
        description=description,
        pending=_read_lines(_state_file(session_id, "pending")),
        search_dirs=search_dirs,
        resolves=lambda s: _skill_resolves(s, search_dirs),
    )
    if not reason:
        return False

    return emit_task_create_deny(reason)


def _resolve_worktree_state(toplevel: str) -> str | None:
    """Return the ticket FSM state for the worktree at on-disk *toplevel*.

    Delegates the path → ``Worktree`` row resolution to the canonical
    :func:`teatree.core.resolve.match_worktree_by_path` (the single source of
    truth for matching an on-disk path against ``extra['worktree_path']``,
    incl. the macOS ``/var`` ↔ ``/private/var`` symlink variants and the
    subdirectory walk) rather than a hand-rolled query — a hand-rolled
    ``Worktree.objects.filter(path=…)`` is exactly the #1957 dead-gate bug:
    ``Worktree`` has no ``path`` field (the on-disk path lives in
    ``extra['worktree_path']``), so every call raised ``FieldError``. Raises on
    a programming error so the caller can log it loudly rather than swallow it
    into a silent fail-open.
    """
    from teatree.core.resolve import match_worktree_by_path  # noqa: PLC0415

    worktree = match_worktree_by_path(toplevel)
    if worktree is None or worktree.ticket is None:
        return None
    return str(worktree.ticket.state)


def _ticket_state_for_cwd(cwd: str) -> str | None:
    """Return the ticket's FSM state for the worktree at *cwd*, or ``None``.

    Resolves the cwd → git toplevel → Worktree DB row → Ticket.state. Fails
    open (returns ``None``) on an OPERATIONAL failure — teatree unavailable,
    cwd not a managed worktree, git/subprocess error — so the hook never wedges
    an agent. A PROGRAMMING error (wrong field name, bad import — the #1957
    class) is NOT swallowed silently: it emits a loud stderr NOTE before the
    fail-open so a dead gate is diagnosable instead of invisible.
    """
    src_dir = Path(__file__).resolve().parents[2] / "src"
    added = False
    try:
        if str(src_dir) not in sys.path:
            sys.path.insert(0, str(src_dir))
            added = True
        import django  # noqa: PLC0415
        from django.core.exceptions import FieldError  # noqa: PLC0415

        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "teatree.settings")
        django.setup()

        try:
            toplevel = subprocess.check_output(  # noqa: S603
                ["git", "-C", cwd, "--no-optional-locks", "rev-parse", "--show-toplevel"],  # noqa: S607
                text=True,
                timeout=3,
                stderr=subprocess.DEVNULL,
            ).strip()
        except (subprocess.SubprocessError, OSError):
            return None
        try:
            return _resolve_worktree_state(toplevel)
        except (FieldError, TypeError, AttributeError, ImportError) as exc:
            # Programming-error class (the #1957 dead-gate root cause): stay
            # crash-proof (return None) but make it LOUD, never a silent ALLOW.
            sys.stderr.write(f"NOTE: plan-gate edit-block resolver hit a programming error ({exc!r}); failing open.\n")
            return None
    except Exception:  # noqa: BLE001
        return None
    finally:
        if added:
            with contextlib.suppress(ValueError):
                sys.path.remove(str(src_dir))


def _plan_edit_gate_enabled() -> bool:
    """Whether the plan-edit gate is enabled (default True).

    Fails OPEN to enabled on a missing/broken config so the gate keeps its
    protective default; an explicit ``false`` is the one-line kill-switch
    (``t3 <overlay> gate plan disable``, never a code edit). See
    :func:`_teatree_bool_setting` for the shared bare-boolean semantics.
    """
    return _teatree_bool_setting("plan_edit_gate_enabled", default=True)


def handle_block_edit_before_planned(data: dict) -> bool:
    """Deny Edit/Write when the worktree's ticket is still in STARTED state.

    The FSM already prevents ``code()`` from STARTED (TransitionNotAllowed),
    so this gate provides an earlier, clearer DX signal: edit attempts while
    the ticket has not yet been planned are denied with an actionable message.
    Fail-open on every resolution failure so the gate never wedges an agent
    when the DB is unavailable or the cwd is not a managed worktree.

    **Never-lockout escapes (mirror the skill-loading gate):**

    1. Per-call token ``[skip-plan-gate: <non-empty-reason>]`` in ``new_string``
        / ``content`` / ``file_path`` (first 512 chars) — the trivial escape.
    2. Config kill-switch ``[teatree] plan_edit_gate_enabled = false`` in
        ``~/.teatree.toml`` (flipped by ``t3 <overlay> gate plan disable``).

    The existing ``_fail_open_or_deny`` safety chain (self-rescue allowlist +
    master ``danger_gate_fail_open``) is unchanged — the escapes above are
    ADDITIONS to it, not replacements.
    """
    tool_name = data.get("tool_name", "")
    if tool_name not in {"Edit", "Write"}:
        return False
    if not _plan_edit_gate_enabled():
        return False
    cwd = data.get("cwd", "") or str(Path.cwd())
    try:
        state = _ticket_state_for_cwd(cwd)
    except Exception:  # noqa: BLE001
        return False
    if state != "started":
        return False
    if reason_token := _skip_plan_gate_token(data):
        sys.stderr.write(f"NOTE: plan-gate edit-block skipped via [skip-plan-gate: {reason_token}].\n")
        return False
    reason = (
        f"{tool_name} denied: the worktree's ticket is still in STARTED state — "
        "a plan must be recorded before coding can begin. "
        "Run the planning phase first so the ticket advances to PLANNED. "
        "If this is a trivial mechanical edit, add `[skip-plan-gate: <reason>]` to proceed."
    )
    return _fail_open_or_deny(data, reason)


# ── PreToolUse: protect-default-branch ─────────────────────────────


_DEFAULT_PROTECTED_BRANCHES = {"main", "master"}


def _load_protected_branches() -> set[str]:
    """Return the merged set of protected branches from defaults + all overlays."""
    import tomllib  # noqa: PLC0415

    branches = set(_DEFAULT_PROTECTED_BRANCHES)
    config_path = Path.home() / ".teatree.toml"
    if not config_path.is_file():
        return branches
    try:
        with config_path.open("rb") as f:
            config = tomllib.load(f)
    except Exception:  # noqa: BLE001
        return branches
    for overlay_cfg in config.get("overlays", {}).values():
        branches.update(overlay_cfg.get("protected_branches", []))
    return branches


# Agent-harness state dirs that may sit UNDER a git repo's working tree
# (e.g. ``~/.claude`` inside a dotfiles repo) but whose files are never
# repo source. A Write here must never be blocked by the protected-branch
# gate — editing agent memory / todos / per-project state on `main` is
# exactly what the agent is supposed to do. Mirrors ``_KEEP_PATTERNS``.
_AGENT_STATE_PATH_RE = re.compile(
    r"/\.(claude|codex|cursor|copilot)/(projects/.*/memory/|memory/|todos/|statsig/|.*\.log$)",
)


def _is_agent_state_path(file_path: str) -> bool:
    """True iff *file_path* is agent-harness state, not repo source.

    Resolved to an absolute, symlink-free path first so a relative or
    ``..``-laden path can't dodge the pattern. A resolution failure (a
    path under a missing dir) falls back to the raw string — the regex
    is anchored on the harness-dir segment, which survives either form.
    """
    try:
        resolved = str(Path(file_path).expanduser().resolve())
    except (OSError, RuntimeError):
        resolved = file_path
    return _AGENT_STATE_PATH_RE.search(resolved) is not None


def _file_is_inside_worktree(repo_root: str, file_path: str) -> bool:
    """True iff *file_path* resolves to a path inside *repo_root*'s working tree.

    ``git -C <parent> rev-parse`` walks UP to the nearest enclosing
    ``.git``, so the resolved repo root can be an ANCESTOR of the file
    (a dotfiles/home repo the file merely sits under). Confirming the
    file is genuinely within that root is what scopes the gate to the
    TARGET FILE's repo rather than whatever happens to enclose its parent
    dir (#126). A resolution failure means we cannot confirm containment —
    fail open (return ``False``, do not block).
    """
    try:
        file_resolved = Path(file_path).expanduser().resolve()
        root_resolved = Path(repo_root).expanduser().resolve()
    except (OSError, RuntimeError):
        return False
    try:
        file_resolved.relative_to(root_resolved)
    except ValueError:
        return False
    return True


def _repo_root_is_teatree_managed(repo_root: str) -> bool:
    """True iff *repo_root* is a teatree-MANAGED source repo.

    The protected-branch gate guards only teatree core + the active
    overlay's registered repos (``~/.teatree.toml``
    ``workspace_repos`` / ``frontend_repos`` / ``public_repos`` slugs,
    plus each overlay ``path``) — NOT every git repo (#126). An unmanaged
    repo on ``main`` (a dotfiles repo, an unrelated clone) must not block,
    so this returns ``False`` for any repo the managed-signal set does not
    cover, and ``False`` on any classification error (fail OPEN — the
    gate-over-deny class this whole change closes).

    Reuses :func:`_overlay_managed_repo_signals` (the same signal source
    as the out-of-band-merge gate) and ``publish_surface.slug_for_cwd``
    so the slug shape matches the rest of the managed-repo machinery.
    """
    slugs, paths = _overlay_managed_repo_signals()
    try:
        root_resolved = Path(repo_root).expanduser().resolve()
    except (OSError, RuntimeError):
        return False
    for base in paths:
        with contextlib.suppress(OSError, RuntimeError):
            root_resolved.relative_to(base)
            return True
    try:
        with _teatree_src_on_path():
            from teatree.hooks import publish_surface  # noqa: PLC0415

            slug = publish_surface.slug_for_cwd(root_resolved).lower()
    except Exception:  # noqa: BLE001
        return False
    return any(entry in slug for entry in slugs) if slug else False


def _resolve_branch_and_root(parent: str) -> tuple[str, str] | None:
    """Return ``(branch, repo_root)`` for the repo enclosing *parent*, or ``None``.

    ``None`` when *parent* is not inside a git repo, on a git error, or on
    a timeout — every one of which fails the gate open. ``git -C`` walks UP
    to the nearest ``.git``, so the returned root can be an ancestor of the
    file; :func:`_file_is_inside_worktree` is what re-scopes it.
    """

    def _rev_parse(*flags: str) -> str:
        return subprocess.check_output(  # noqa: S603
            ["git", "-C", parent, "--no-optional-locks", "rev-parse", *flags],  # noqa: S607
            text=True,
            timeout=3,
            stderr=subprocess.DEVNULL,
        ).strip()

    try:
        return _rev_parse("--abbrev-ref", "HEAD"), _rev_parse("--show-toplevel")
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return None


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

# The ``glab mr create``/``update`` inline/file/dynamic title & description
# parsing lives in the bare sibling module ``mr_cli_fields`` (split out for
# module health); ``extract_cli_mr_fields`` is imported above. The REST-API
# surface and the target-repo parsing stay here.
# REST-API field args set on a ``glab api``/``gh api`` MR/PR write
# (``--field title=…`` / ``-f description=…`` / ``--raw-field …``). Three
# shapes, in order:
#   1. whole token quoted — ``--field 'description=multi word …'`` (the common
#      shell form; the value runs to the matching CLOSING outer quote, so
#      embedded spaces and newlines are kept);
#   2. value quoted only — ``--field description='multi word …'``;
#   3. bare value — ``--field description=oneword`` (runs to next whitespace).
# ``body`` is GitHub's PR-description field (``gh api … -f body=…``); it is
# normalised to ``description`` so the overlay validator sees one key.
_API_FIELD_RE = re.compile(
    r"""(?:--field|--raw-field|-f|-F)[ =]+"""
    r"""(?:(?P<oq>['"])(?P<key>title|description|body)=(?P<oqval>.*?)(?P=oq)"""
    r"""|(?P<key2>title|description|body)=(?:(?P<q>['"])(?P<qval>.*?)(?P=q)|(?P<bval>[^\s'"]*)))""",
    re.DOTALL,
)
# MR title/description value parsing and TARGET-repo slug parsing moved to
# mr_cli_fields (module health) — see extract_cli_mr_fields / extract_mr_target_repo.


def _extract_api_mr_fields(command: str) -> tuple[str, str] | None:
    """Title/description for an out-of-band ``glab api``/``gh api`` MR write.

    Closes the gap where a non-compliant title/description reaches GitLab via
    ``glab api --method PUT .../merge_requests/N --field description=…`` (or a
    ``gh api`` POST), entirely outside the ``glab mr create`` surface the gate
    historically watched. Validates ONLY the fields the command actually sets.

    Neither field set (e.g. ``--field state_event=close``): returns ``None`` —
    nothing to validate (never-lockout: a partial state edit must not be
    force-validated against an empty description). Exactly one field set: the
    untouched field is back-filled with the set field's value as a known-good
    placeholder so the verdict reflects ONLY the field under edit. A valid
    ``type(scope): … (ticket_url)`` line is, by the canonical grammar,
    simultaneously a valid title and a valid description first line — so
    mirroring the set field can never inject a spurious failure for the
    untouched field, while a non-compliant edited field is still rejected
    (without this, editing only the description would false-block on
    ``Title is empty.``). Both fields set: validated as a pair, like a create.

    Reuses :func:`_is_api_create_endpoint_write` so a bare ``GET`` read is
    never treated as a write.
    """
    if not re.search(r"\b(?:gh|glab)\s+api\b", command):
        return None
    if not _is_api_create_endpoint_write(command):
        return None
    fields: dict[str, str] = {}
    for m in _API_FIELD_RE.finditer(command):
        if m.group("oq"):
            key, value = m.group("key"), (m.group("oqval") or "")
        else:
            key = m.group("key2")
            value = (m.group("qval") if m.group("q") else m.group("bval")) or ""
        fields["description" if key == "body" else key] = value
    if not fields:
        return None
    title = fields.get("title")
    description = fields.get("description")
    if title is None:
        title = description or ""
    if description is None:
        description = title
    return title, description


def _extract_mr_fields(data: dict) -> tuple[str, str] | None:
    """Return ``(title, description)`` for an MR create/update, else ``None``.

    ``None`` means "not an MR-metadata mutation" — nothing to validate. A
    returned tuple means the command IS an MR mutation and must be validated
    *even if title/description are empty* — an empty/missing title is exactly
    the kind of bad metadata the gate must reject, not silently pass (#119).

    Covers four surfaces so a non-compliant title/description cannot slip onto
    the forge through any of them:

    1.  ``glab mr create/update --title/--description`` (inline quotes), via
        :func:`extract_cli_mr_fields`. ``create`` validates both fields;
        ``update`` validates ONLY the field(s) it sets (a metadata-only
        reviewer/label/state edit is skipped — never-lockout).
    2.  The same command's file-based / heredoc description
        (``-F``/``--description-file``) — read via :func:`_read_message_file`
        instead of passed through as a falsely-empty string (the slip class: a
        multi-line prose description whose first line was not the
        ``type(scope): … (ticket_url)`` form). A double-quoted ``$(…)``/``$VAR``
        the hook cannot resolve before shell expansion is SKIPPED, never
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
        # and returns the fields, or None when it is not a CLI mutation.
        cli_fields = extract_cli_mr_fields(command)
        if cli_fields is not None:
            return cli_fields
        return _extract_api_mr_fields(command)

    if tool_name in _MR_TOOLS:
        return tool_input.get("title", ""), tool_input.get("description", "")

    return None


def _mr_validate_argv() -> list[str] | None:
    """Resolve the command that validates MR metadata.

    Default (no opt-in): ``t3 tool validate-mr`` — runs the active
    overlay's ``validate_pr``, the same verdict ``t3 <overlay> pr create``
    uses, so a bad title/description is rejected BEFORE the push every time
    (#119). ``T3_MR_VALIDATE_SCRIPT`` remains an explicit override escape
    hatch. Returns ``None`` when no validator is resolvable (fail open —
    don't block the agent on a broken environment, matching the other
    t3-shelling hooks).
    """
    script = os.environ.get("T3_MR_VALIDATE_SCRIPT", "")
    if script and Path(script).is_file():
        return ["python3", script]
    t3_bin = shutil.which("t3")
    if t3_bin:
        return [t3_bin, "tool", "validate-mr"]
    return None


_MR_VALIDATE_BROKEN_ENV_DENY = (
    "Cannot validate MR title/description — the overlay validator "
    "(`t3 tool validate-mr`) is not resolvable or crashed. Refusing to create "
    "the MR with unvalidated metadata (fail closed). Fix the environment, or "
    "set T3_MR_VALIDATE_ALLOW_BROKEN_ENV=1 to deliberately bypass."
)


def _handle_broken_validate_env(data: dict) -> bool:
    """Decide the gate's action when the validator can't run.

    The MR-metadata gate FAILS CLOSED by default (deny): a non-compliant title
    must never reach GitLab just because the env could not validate it. The
    explicit ``T3_MR_VALIDATE_ALLOW_BROKEN_ENV`` opt-in is the per-gate
    self-rescue, and the broken-env deny additionally routes through
    :func:`_fail_open_or_deny` so the master ``danger_gate_fail_open`` switch and
    the always-allowed self-rescue commands relax it too (NEVER-LOCKOUT).
    """
    if os.environ.get("T3_MR_VALIDATE_ALLOW_BROKEN_ENV", "").strip().lower() in {"1", "true", "yes"}:
        return False
    return _fail_open_or_deny(data, _MR_VALIDATE_BROKEN_ENV_DENY)


def _run_mr_validator(
    argv: list[str], title: str, description: str, target_repo: str | None = None
) -> "subprocess.CompletedProcess[str] | None":
    """Run the validator, or ``None`` if the env is broken (timeout/missing).

    ``target_repo`` (when parseable from the command) is forwarded as
    ``--repo <slug>`` so the validator keys overlay resolution to the MR's
    TARGET, not the agent's cwd — the whole point of the target-keyed gate.
    """
    repo_args = ["--repo", target_repo] if target_repo else []
    try:
        return subprocess.run(  # noqa: S603
            [*argv, "--title", title, "--description", description, *repo_args],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None


def handle_validate_mr_metadata(data: dict) -> bool:
    """Block a non-compliant ``glab mr``/``gh pr`` create/update before it runs.

    Validates by default via the TARGET overlay's ``validate_pr`` (no env-var
    opt-in) so the pre-push gate is always live (#119 Part 3). The MR's TARGET
    repo is parsed from the command (``-R``/``--repo``, the ``glab api``
    namespace, the ``gh api repos/<o>/<r>`` path) and threaded as ``--repo`` so
    an MR targeting a stricter-rule overlay, created with cwd in a repo owned by
    a more-lenient overlay, is graded against the TARGET overlay's rules — not
    the cwd overlay's weaker ones. When the validator cannot be resolved or
    crashes, the gate FAILS CLOSED — a non-compliant
    title must never slip onto the forge on a broken env. The explicit
    ``T3_MR_VALIDATE_ALLOW_BROKEN_ENV`` opt-in restores fail-open as a
    deliberate self-rescue.
    """
    fields = _extract_mr_fields(data)
    if fields is None:
        return False
    title, description = fields
    target_repo = (
        extract_mr_target_repo(data.get("tool_input", {}).get("command", ""))
        if data.get("tool_name") == "Bash"
        else None
    )

    argv = _mr_validate_argv()
    if argv is None:
        return _handle_broken_validate_env(data)

    result = _run_mr_validator(argv, title, description, target_repo)
    if result is None:
        return _handle_broken_validate_env(data)

    if result.returncode != 0:
        return emit_pretooluse_deny(
            (result.stderr or result.stdout or "").strip() or "MR title/description failed overlay validation."
        )
    return False


# ── PreToolUse: block-ai-signature (#836 §17.6 gate 15) ─────────────

_PR_CREATE_TOOLS = {
    "mcp__glab__glab_mr_create",
    "mcp__glab__glab_mr_update",
    "mcp__github__create_pull_request",
    "mcp__github__update_pull_request",
}


# REST-API create-endpoint: .../pulls or .../merge_requests WITHOUT /N/merge.
# Distinguishes a PR/MR create from a list read (GET) or the merge endpoint
# already covered by _MERGE_ENDPOINT_RE.  The optional /\d+ matches both the
# collection endpoint (/pulls, /merge_requests) and a per-MR update endpoint
# (/pulls/42, /merge_requests/42) when written as a POST.
#
# The trailing class keeps `/` so the collection-create form written WITH a
# trailing slash (`/merge_requests/ -f title=…`) still matches as a create —
# dropping `/` here lets a real trailing-slash MR/PR-create POST escape all
# three consumers. The sub-resource exclusion lives entirely in the lookahead
# `(?!/\d*/?[A-Za-z])`: a read-only nested GET (`/merge_requests/42/approvals`,
# `/pulls/123/commits`, `/notes`, `/files`, `/pipelines`) is `/\d+` then `/`
# then a letter, so the lookahead rejects it; the trailing-slash create is
# `/` then a space (not a letter), so the lookahead admits it.
_API_CREATE_ENDPOINT_RE = re.compile(r"/(?:pulls|merge_requests)(?:/\d+)?(?!/\d*/?[A-Za-z])(?:[/?'\"\s]|$)")


def _is_api_create_endpoint_write(command: str) -> bool:
    """Whether *command* is a REST-API POST/PATCH to a PR/MR collection endpoint.

    True only when the command targets a ``.../pulls`` or
    ``.../merge_requests`` endpoint (without the ``/N/merge`` suffix already
    covered by :data:`_MERGE_ENDPOINT_RE`) AND its effective HTTP method is
    not GET.  Reuses the gate-3 effective-method classifier (last
    ``-X``/``--method`` wins; default POST with a body flag, else GET).
    A bare GET to the list endpoint reads PR list and must NOT be treated as
    a create-class mutation.
    """
    if not _API_CREATE_ENDPOINT_RE.search(command):
        return False
    # Exclude the merge endpoint (already handled by out-of-band-merge gate).
    if _MERGE_ENDPOINT_RE.search(command):
        return False
    return _effective_method_is_write(command)


def _effective_method_is_write(command: str) -> bool:
    """Whether a gh/glab REST command's EFFECTIVE HTTP method is a write (not GET).

    The LAST ``-X``/``--method`` value wins; with no method flag the forge
    defaults to POST when a body/field flag is present, else GET. A GET is the
    only read. Shared by the create-endpoint and merge-endpoint gates so the
    classifier cannot drift between them.
    """
    methods = [m.upper() for pair in _REVIEW_POST_METHOD_RE.findall(command) for m in pair if m]
    if methods:
        return methods[-1] != "GET"
    return bool(_REVIEW_POST_BODY_FLAG_RE.search(command))


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
    from teatree.hooks.ai_signature_gate import extract_forge_post_body  # noqa: PLC0415

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
    if t3_bin:
        return [t3_bin, "tool", "ai-sig-scan", "-"]
    return None


# A genuine finding is recognisable by the scanner's well-formed summary
# header ``AI-signature scan: N banned trailer(s)`` (``scripts/
# ai_signature_scan.py`` ``_summary``). The scanner exits 1 on a finding AND
# nonzero on a crash (a missing/unreadable ``-F`` file → typer traceback →
# exit 1, no summary on stdout), so ``returncode != 0`` alone CANNOT tell the
# two apart — keying on the summary line does, mirroring the sibling
# ``_diff_coverage_finding`` structured-stdout discriminator.
_AI_SIG_FINDING_RE = re.compile(r"^AI-signature scan:\s+\d+\s+banned trailer", re.MULTILINE)


def _ai_sig_finding(stdout: str) -> str | None:
    """Return the finding summary iff *stdout* is a real banned-trailer finding.

    ``None`` ⇒ not a genuine finding: either the clean summary (``AI-signature
    scan: clean``) or a crash/error with no well-formed summary at all. The
    caller maps the three outcomes to DENY-finding / ALLOW / fail-closed-error.
    """
    if _AI_SIG_FINDING_RE.search(stdout):
        return stdout.strip()
    return None


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
    if payload is None:
        return False

    argv = _ai_sig_scan_argv()
    if argv is None:
        return False

    try:
        result = subprocess.run(  # noqa: S603
            argv,
            input=payload,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False

    finding = _ai_sig_finding(result.stdout or "")
    if finding is not None:
        return emit_pretooluse_deny(
            "BLOCKED: AI-signature / banned trailer in the PR body or commit message. "
            "Remove it before creating the PR/commit (BLUEPRINT §17.6 gate 15).\n" + finding
        )
    if result.returncode != 0:
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
    return False


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


def _is_slack_mcp_tool(tool_name: str) -> bool:
    """Whether *tool_name* is a Slack MCP tool (``mcp__*slack*``).

    The kill-switch governs ONLY the Slack-MCP arm of the publish-privacy
    gates; the Bash arm stays live regardless. This mirrors the matcher
    ``mcp__.*[Ss]lack.*`` so the canary off-switch scopes to exactly the
    newly-reachable arm.
    """
    return tool_name.startswith("mcp__") and "slack" in tool_name.lower()


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
    if _is_slack_mcp_tool(data.get("tool_name", "")) and not _mcp_privacy_gate_enabled():
        return False
    src_dir = Path(__file__).resolve().parents[2] / "src"
    added = False
    try:
        if str(src_dir) not in sys.path:
            sys.path.insert(0, str(src_dir))
            added = True
        return _run_quote_scanner_pretool(data)
    except Exception:  # noqa: BLE001
        return False
    finally:
        if added:
            with contextlib.suppress(ValueError):
                sys.path.remove(str(src_dir))


def _quote_scanner_high_verdict(
    quote_scanner: "ModuleType", tool_name: str, result: object, *, carve_out: bool
) -> bool:
    """Resolve a HIGH quote-scanner match into a deny / downgrade verdict.

    A HIGH match on a private-repo commit (``carve_out``) downgrades to a
    warn (#126); every other HIGH match denies. Split out of
    :func:`_run_quote_scanner_pretool` to keep its return count under the
    PLR0911 ceiling.
    """
    if carve_out:
        sys.stderr.write(
            "WARNING: pre-publish quote-scanner gate (#1213) — patterns matched on a "
            "private-repo commit; downgraded to warn (#126). Verify the content is paraphrased.\n"
        )
        quote_scanner.log_decision(tool_name=tool_name, decision="warn-private-repo", result=result, override=False)
        return False
    quote_scanner.log_decision(tool_name=tool_name, decision="deny", result=result, override=False)
    return emit_pretooluse_deny(quote_scanner.format_block_message(result))


def _run_quote_scanner_pretool(data: dict) -> bool:
    """Quote-scanner inner body — assumes ``teatree`` is already importable.

    Split out of :func:`handle_quote_scanner_pretool` so the outer
    wrapper owns the ``sys.path`` bootstrap + fail-open exception
    handler (#1314) without inflating its return count.
    """
    from typing import cast  # noqa: PLC0415

    from teatree.hooks import publish_surface, quote_scanner  # noqa: PLC0415

    tool_name = data.get("tool_name", "")
    raw_input = data.get("tool_input", {}) or {}
    if not isinstance(raw_input, dict):
        return False
    tool_input = cast("quote_scanner.ToolInput", raw_input)

    payload = quote_scanner.extract_publish_payload(tool_name, tool_input)
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
        carve_out = publish_surface.carve_out_applies(tool_name, command, payload, _resolve_cwd_repo(data))
        return _quote_scanner_high_verdict(quote_scanner, tool_name, result, carve_out=carve_out)

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
_SELF_DM_CHANNEL_FIELDS: tuple[str, ...] = ("channel", "channel_id")


def _slack_tool_suffix(tool_name: str) -> str:
    return tool_name.rsplit("__", 1)[-1]


def _self_dm_gate_enabled() -> bool:
    """Whether the self-DM gate is enabled (default True).

    Fails OPEN to enabled on a missing/broken config; an explicit ``false``
    is the one-line kill-switch. See :func:`_teatree_bool_setting` for the
    shared bare-boolean semantics.
    """
    return _teatree_bool_setting("self_dm_gate_enabled", default=True)


@dataclasses.dataclass(frozen=True)
class _SelfDmDestinations:
    """Resolved set of self-DM destination ids, with a read-success flag.

    The set mirrors the canonical ``SlackBotBackend._is_self_dm``: each
    overlay's ``slack_dm_channel_id`` (the ``D…`` self-IM id) AND each
    ``slack_user_id`` plus the global ``[teatree] slack_user_id`` (the
    ``U…`` id Slack accepts as a target that opens the self-IM).

    ``resolved`` distinguishes a genuinely-empty configuration (nothing
    declared → ALLOW silently) from an unreadable/unparsable one
    (→ DENY fail-closed: the hook cannot self-identify the author without the
    config, so a can't-read config must not let a self-DM through).
    """

    ids: frozenset[str]
    resolved: bool


def _self_dm_destination_ids() -> _SelfDmDestinations:
    import tomllib  # noqa: PLC0415

    config_path = Path.home() / ".teatree.toml"
    if not config_path.is_file():
        return _SelfDmDestinations(frozenset(), resolved=False)
    try:
        with config_path.open("rb") as f:
            config = tomllib.load(f)
    except Exception:  # noqa: BLE001
        return _SelfDmDestinations(frozenset(), resolved=False)
    ids: set[str] = set()
    overlays = config.get("overlays")
    if isinstance(overlays, dict):
        for cfg in overlays.values():
            if not isinstance(cfg, dict):
                continue
            for key in ("slack_dm_channel_id", "slack_user_id"):
                value = cfg.get(key)
                if isinstance(value, str) and value:
                    ids.add(value)
    teatree = config.get("teatree")
    if isinstance(teatree, dict) and isinstance(teatree.get("slack_user_id"), str) and teatree["slack_user_id"]:
        ids.add(teatree["slack_user_id"])
    return _SelfDmDestinations(frozenset(ids), resolved=True)


def _self_dm_destination(tool_input: dict, dm_ids: frozenset[str]) -> str:
    for field in _SELF_DM_CHANNEL_FIELDS:
        value = tool_input.get(field)
        if isinstance(value, str) and value in dm_ids:
            return value
    return ""


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
    subprocess, and the tool-schema text is not part of the hook input), so a
    missing/unreadable/unparsable config DENIES with an error naming the toml
    problem and the fix. A genuinely-empty configuration (config readable,
    nothing declared) is a real state, not an error, so it allows silently. The
    ``[teatree] self_dm_gate_enabled = false`` setting is the sanctioned
    explicit escape hatch (never a silent one).
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
            "from ~/.teatree.toml (the file is missing or not valid TOML), so this gate "
            "cannot confirm the Slack MCP write is not a self-DM under the USER's OAuth "
            "token. Fix the ~/.teatree.toml so it parses (with the per-overlay "
            "slack_dm_channel_id / slack_user_id keys), or set [teatree] "
            "self_dm_gate_enabled = false to disable this gate explicitly. To DM the user "
            "now, use the bot-token path: `t3 teatree notify send -` (reads the body from stdin)."
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
    mandatory), mirroring the ``[skip-skill-gate: <reason>]`` convention —
    the publish-side ``--quote-ok`` flag / ``QUOTE_OK=1`` env have no
    analogue inside a prompt body.

    Fail-open on any internal error (a crashing gate is worse than no
    scan): the ``sys.path`` bootstrap + exception swallow mirror the #1314
    posture of the publish gate. Every decision lands in the shared
    quote-scanner ledger so cold review can audit what the gate saw.
    """
    src_dir = Path(__file__).resolve().parents[2] / "src"
    added = False
    try:
        if str(src_dir) not in sys.path:
            sys.path.insert(0, str(src_dir))
            added = True
        return _run_dispatch_quote_scanner(data)
    except Exception:  # noqa: BLE001
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
    from typing import cast  # noqa: PLC0415

    from teatree.hooks import quote_scanner  # noqa: PLC0415

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


# ── TaskCreated: pre-dispatch quote-scanner gate (#171, fan-out arm) ─


def _dispatch_quote_gate_on_task_create_enabled() -> bool:
    """Whether the TaskCreated dispatch-quote gate is enabled (default OFF, opt-in).

    The PreToolUse dispatch-quote gate (:func:`handle_dispatch_prompt_quote_scanner`)
    keys on ``Agent``/``Task``, but the harness Workflow/Task fan-out — where
    dispatch prompts are actually created — BYPASSES ``PreToolUse``, so that
    gate never fires on the real dispatch path. This ``TaskCreated`` counterpart
    closes that bypass. It ships default-OFF because it is a #1640-class fan-out
    gate whose live behavior is unvalidated: an unvalidated gate stays inert
    (never wedges the loop) until the operator deliberately enables it with
    ``[teatree] dispatch_quote_gate_on_task_create_enabled = true``.

    Fails CLOSED to disabled (missing config → False, broken → False) and returns
    True only on an explicit ``true``. This deliberately DIFFERS from
    :func:`_mcp_privacy_gate_enabled` (which fails OPEN to enabled): the Slack-MCP
    arm is the same risk class as an already-live gate, whereas this fan-out
    gate's enforcement semantics are not yet validated. See
    :func:`_teatree_bool_setting` for the shared bare-boolean semantics.
    """
    return _teatree_bool_setting("dispatch_quote_gate_on_task_create_enabled", default=False)


def handle_dispatch_prompt_quote_scanner_on_task_create(data: dict) -> bool:
    """Deny a fanned-out ``Task`` whose subject/description carries a HIGH verbatim quote.

    Closes the fan-out loophole in :func:`handle_dispatch_prompt_quote_scanner`:
    the ``PreToolUse`` Agent/Task dispatch-quote gate is skipped on the
    Workflow/Task fan-out path (only ``TaskCreated`` reaches it), so a verbatim
    user-voice/PII fragment pasted into a fan-out brief as "context" would reach
    the sub-agent and could later be echoed into a published output — defeating
    the #1213 publish gate. This handler scans the ``task_subject`` +
    ``task_description`` through the SAME ``quote_scanner.scan_text`` detector
    (HIGH-severity deny only, mirroring the PreToolUse handler) before the
    sub-agent is spawned.

    NEVER-LOCKOUT:
    this does NOT route through ``_fail_open_or_deny`` / ``_is_self_rescue``
    (those are PreToolUse/Bash-command-shaped; a ``TaskCreated`` event carries no
    command). The gate ships default-OFF (opt-in via ``[teatree]
    dispatch_quote_gate_on_task_create_enabled = true``) — a #1640-class fan-out
    gate whose live behavior is unvalidated stays inert by default. When enabled,
    the off-ramps that keep the operator from being locked out are: the opt-in
    flag itself (unset/``false`` to disable), the ``[quote-ok: <reason>]`` token
    in the subject/description (reuses :func:`quote_scanner.dispatch_quote_ok_reason`),
    a missing ``session_id`` (fail-open), a broken ``~/.teatree.toml``
    (fail-disabled), and ``main``'s per-handler exception swallow. The master
    ``danger_gate_fail_open`` switch still protects the operator because rescue
    commands run as ``Bash``, never as fanned-out ``Task``s.
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
    except Exception:  # noqa: BLE001
        return False
    finally:
        if added:
            with contextlib.suppress(ValueError):
                sys.path.remove(str(src_dir))


def _run_dispatch_quote_scanner_on_task_create(data: dict) -> bool:
    """TaskCreated dispatch-quote inner body — assumes ``teatree`` is importable.

    Split out of :func:`handle_dispatch_prompt_quote_scanner_on_task_create` so
    the outer wrapper owns the ``sys.path`` bootstrap + fail-open handler
    (mirrors the #1213/#1401 split). A HIGH match emits the ``TaskCreated``
    teammate-stop deny envelope (NOT the PreToolUse ``hookSpecificOutput`` deny).
    """
    from teatree.hooks import quote_scanner  # noqa: PLC0415

    subject = data.get("task_subject", "") or ""
    description = data.get("task_description", "") or ""
    payload = f"{subject}\n{description}"

    if quote_scanner.dispatch_quote_ok_reason(payload):
        quote_scanner.log_decision(
            tool_name="TaskCreated:dispatch",
            decision="allow-override",
            result=quote_scanner.scan_text(payload),
            override=True,
        )
        return False

    result = quote_scanner.scan_text(payload)
    if result.has_high:
        quote_scanner.log_decision(
            tool_name="TaskCreated:dispatch",
            decision="deny",
            result=result,
            override=False,
        )
        return emit_task_create_deny(quote_scanner.format_dispatch_block_message(result))

    quote_scanner.log_decision(
        tool_name="TaskCreated:dispatch",
        decision="allow",
        result=result,
        override=False,
    )
    return False


# ── PreToolUse: banned-terms posting gate (#1415) ───────────────────


def _banned_terms_gate_enabled() -> bool:
    """Whether the #1415 banned-terms publish gate is enabled (default True).

    Fails OPEN to enabled on a missing/broken config so the gate keeps its
    protective default; an explicit ``[teatree] banned_terms_gate_enabled =
    false`` is the one-line kill-switch (NEVER-LOCKOUT) the user flips to
    disable the gate while its body-resolution over-block (an allowlisted
    private-repo commit hard-blocked because the body could not be read) is
    fixed properly. See :func:`_teatree_bool_setting` for the shared semantics.
    """
    return _teatree_bool_setting("banned_terms_gate_enabled", default=True)


def handle_banned_terms_pretool(data: dict) -> bool:
    """Refuse a non-commit publish whose body carries a banned term.

    Sibling of the #1213 quote-scanner gate. The commit-only
    ``check-banned-terms.sh`` pre-commit hook misses ``gh issue/pr
    create|edit|comment``, ``glab mr|issue note|create`` and the
    ``gh api`` / ``glab api`` REST posting paths — exactly where
    overlay/customer terms have leaked on this PUBLIC repo. This gate
    reuses the #1213 ``_command_parser`` publish-surface detection + body
    extraction, then delegates the matching to the SAME
    ``check-banned-terms.sh`` against the ``~/.teatree.toml`` term list
    (no new term config, no reimplemented matching).

    A banned-term match ⇒ refuse via ``permissionDecision: deny`` + a
    reason naming the matched term and pointing at the
    ``--allow-banned-term`` / ``ALLOW_BANNED_TERM=1`` override.

    Fail-open on any internal error: a crashing hook is worse than no
    scan. The handler bootstraps ``sys.path`` to import ``teatree`` from
    the sibling ``src/`` directory (the hook script runs in the user's
    session shell with no guarantee that ``teatree`` is already
    importable, #1314) and swallows any exception, returning ``False``.
    """
    if not _banned_terms_gate_enabled():
        return False
    src_dir = Path(__file__).resolve().parents[2] / "src"
    added = False
    try:
        if str(src_dir) not in sys.path:
            sys.path.insert(0, str(src_dir))
            added = True
        return _run_banned_terms_pretool(data)
    except Exception:  # noqa: BLE001
        return False
    finally:
        if added:
            with contextlib.suppress(ValueError):
                sys.path.remove(str(src_dir))


_BANNED_TERMS_CREDENTIAL_DENY = (
    "BLOCKED: a high-confidence secret (token / key / private-key block) was detected in the "
    "publish payload. Secrets are blocked on every surface, including a private repo — remove "
    "the credential before posting."
)


def _banned_term_marker_blocks(term: str, command: str, cwd_repo: "Path | None") -> bool | None:
    """Decide a fail-closed MARKER term, or ``None`` when ``term`` is a real banned term.

    Thin router wrapper over ``banned_terms_marker.resolve_marker`` (which owns the
    destination-aware logic + rationale). For a real configured term it returns
    ``None`` so the caller takes its own destination-aware banned-term path. For a
    fail-closed marker the verdict is either a downgrade-to-warn (write the stderr
    line, return ``False``) or a hard-block (``emit_pretooluse_deny``).
    """
    verdict = _resolve_banned_terms_marker(term, command, cwd_repo)
    if not verdict.is_marker:
        return None
    if verdict.warning is not None:
        sys.stderr.write(verdict.warning)
        return False
    return emit_pretooluse_deny(verdict.deny_message or "")


def _run_banned_terms_pretool(data: dict) -> bool:
    """Banned-terms inner body — assumes ``teatree`` is already importable."""
    from typing import cast  # noqa: PLC0415

    from teatree.hooks import banned_terms_scanner, publish_destination, publish_surface  # noqa: PLC0415

    tool_name = data.get("tool_name", "")
    raw_input = data.get("tool_input", {}) or {}
    if not isinstance(raw_input, dict):
        return False
    tool_input = cast("banned_terms_scanner.ToolInput", raw_input)

    command = tool_input.get("command", "")
    cwd_repo = _resolve_cwd_repo(data)

    # A high-confidence secret leaks on EVERY surface -- a title, a short ``-t``
    # flag, a ``gh api -f title=`` field, a ``git -C ... commit`` subject -- not
    # only the description body, and on an internal post the destination gate
    # would SKIP or a command carrying the --allow-banned-term override. Scan the
    # WIDE surface set and block before the payload-None early-return and any skip
    # / override short-circuit (#1672 secrets-always-blocked invariant).
    if publish_surface.contains_secret(banned_terms_scanner.secret_scan_text(tool_name, tool_input)):
        return emit_pretooluse_deny(_BANNED_TERMS_CREDENTIAL_DENY)

    payload = banned_terms_scanner.extract_publish_payload(tool_name, tool_input, cwd_repo)
    if payload is None:
        return False

    skipped = banned_terms_scanner.has_override(tool_name, tool_input) or (
        tool_name == "Bash" and publish_destination.gate_skips_destination(command, cwd_repo)
    )
    term = None if skipped else banned_terms_scanner.scan_text(payload)
    if term is None:
        return False
    marker_decision = _banned_term_marker_blocks(term, command, cwd_repo)
    if marker_decision is not None:
        return marker_decision
    return emit_banned_term_deny(tool_name, command, payload, term, cwd_repo)


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

_GH_PR_READY_RE = re.compile(r"\bgh\s+pr\s+ready\b")
_PR_MR_CREATE_RE = re.compile(r"\b(?:gh\s+pr\s+create|glab\s+mr\s+create)\b")
_DRAFT_FLAG_RE = re.compile(r"(?:^|\s)(?:--draft|--undo)\b")


def _is_merge_class_mutation(data: dict) -> bool:
    """Whether this tool call moves a PR toward review/merge.

    ``gh pr ready`` (un-drafting) or a non-draft ``gh pr create`` /
    ``glab mr create`` or a ``gh api``/``glab api`` POST to a PR/MR
    collection endpoint (F2 — same semantic effect, same gate coverage
    needed). ``gh pr ready --undo`` (return-to-draft, the gate's own
    remediation) and ``--draft`` creation are excluded.
    """
    if data.get("tool_name") != "Bash":
        return False
    command = data.get("tool_input", {}).get("command", "")
    if _GH_PR_READY_RE.search(command):
        return not _DRAFT_FLAG_RE.search(command)
    if _PR_MR_CREATE_RE.search(command):
        return not _DRAFT_FLAG_RE.search(command)
    # F2: gh/glab api POST to a PR/MR create endpoint is merge-class too.
    if re.search(r"\b(?:gh|glab)\s+api\b", command) and _is_api_create_endpoint_write(command):
        return not _DRAFT_FLAG_RE.search(command)
    return False


def _diff_coverage_argv() -> list[str] | None:
    t3_bin = shutil.which("t3")
    if t3_bin:
        return [t3_bin, "tool", "diff-coverage", "--json"]
    return None


def _diff_coverage_finding(stdout: str) -> str | None:
    """Return a deny reason iff *stdout* is a report JSON with ``passes`` false.

    The fail-open discriminator (#122). ``t3 tool diff-coverage --json``
    emits exactly ``{"passes": ..., "uncovered": [...],
    "unreferenced_symbols": [...]}`` on a successful measurement. A crash
    (e.g. the dev-only ``coverage`` module missing from the installed
    ``t3`` env) produces a traceback on stderr and no parseable JSON on
    stdout — so anything that is not a well-formed report with
    ``passes is False`` is "not a finding" and the caller fails open.

    Returns the human-readable finding summary when there IS a genuine
    finding, else ``None`` (clean, crashed, or unparsable).
    """
    try:
        report = json.loads(stdout)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(report, dict) or report.get("passes") is not False:
        return None
    rows = [
        f"  uncovered new lines in {entry.get('path')}: {entry.get('lines')}"
        for entry in (report.get("uncovered") or [])
        if isinstance(entry, dict)
    ]
    symbols = report.get("unreferenced_symbols") or []
    if symbols:
        rows.append(f"  new production symbols not referenced by any changed test: {sorted(symbols)}")
    return "\n".join(rows)


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

    argv = _diff_coverage_argv()
    if argv is None:
        return False

    try:
        result = subprocess.run(  # noqa: S603
            argv,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False

    finding = _diff_coverage_finding(result.stdout or "")
    if finding is None:
        return False

    return _fail_open_or_deny(
        data,
        "BLOCKED: per-diff coverage gate 12 failed (BLUEPRINT §17.6.3). "
        "An added production line is uncovered or a changed symbol is not "
        "referenced by a changed test. Cover/reference it, then re-mark the "
        "PR ready (resolve the finding before re-requesting review).\n" + finding,
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
# Main-vs-sub-agent signal (#115 root cause). The PreToolUse payload's
# ``transcript_path`` ALWAYS points at the PARENT session transcript,
# even for a sub-agent's tool call (a sub-agent's own turns live in a
# separate ``…/subagents/agent-<id>.jsonl`` the hook never receives), and
# the parent transcript's tail entries carry ``isSidechain: false`` — so
# the previous transcript-``isSidechain`` read MISDETECTED every genuine
# sub-agent as the main agent and blocked it. The reliable signal is on
# the payload itself: a sub-agent call carries a non-empty ``agent_id``
# (and ``agent_type``); a main-agent call omits it. ``_call_is_from_subagent``
# reads that field directly — no transcript needed.

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
# ``pytest`` must match only in a VERB POSITION — never inside a quoted
# arg, a branch name, a ``-m``/``--title`` message, or a hyphenated
# package name (``pytest-django``). A bare ``\bpytest\b`` mis-denied the
# loop owner's ``git commit -m 'fix pytest fixture'`` / ``git branch
# x-pytest`` / ``uv add pytest-django`` (#1178 cold-review false-deny).
# So anchor it to a command head: start-of-string OR a shell separator
# (``;`` ``&&`` ``||`` ``|`` newline ``(`` ``{``), then optional env-var
# assignments, optional (possibly-stacked) command-wrapper prefixes
# (``command``/``exec``/``time``/``nice``), and an optional Python runner
# prefix — note ``uvx`` runs a tool DIRECTLY with no ``run`` (``uvx
# pytest``), while ``uv``/``poetry``/``pdm``/``hatch`` DO need ``run``, and
# ``python[3] -m`` — then ``pytest`` NOT followed by a word char or hyphen.
# The separator branch keeps the shell-grammar bypass guard intact (``git
# status && pytest`` still denies); the trailing ``(?![\w-])`` keeps the
# match pinned to ``pytest`` so wrapper prefixes never widen to other tools
# (``uvx ruff`` / ``command ls`` stay ALLOWED).
_PYTEST_VERB_RE = (
    r"(?:^|[;&|\n(){}])"
    r"\s*"
    r"(?:\w+=\S+\s+)*"
    r"(?:(?:command|exec|time|nice)\s+)*"
    r"(?:uvx\s+|(?:uv|poetry|pdm|hatch)\s+run\s+|python3?\s+-m\s+)?"
    r"pytest(?![\w-])"
)
_PYTEST_VERB_FINDER = re.compile(_PYTEST_VERB_RE)

# A TARGETED pytest run is cheap and must stay ALLOWED in the foreground
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
# A foreground ``git push`` runs the full pre-push suite and wedges the
# loop owner's session (#1825 motivating incident). Read-only git
# (``status``/``log``/``diff``/``show``/``fetch``) is NOT here — only the
# push verb (and its ``--force*`` variants) denies. Anchored to a command
# head the same way the pytest verb is, so a ``git commit -m 'push fix'``
# / ``git branch push-x`` mention is NOT a false-deny.
_GIT_PUSH_RE = (
    r"(?:^|[;&|\n(){}])"
    r"\s*"
    r"(?:\w+=\S+\s+)*"
    r"(?:(?:command|exec|time|nice)\s+)*"
    r"git\s+(?:-C\s+\S+\s+|--git-dir[=\s]\S+\s+)*push\b"
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
# full-tree recursive sweeps (the shapes that actually wedge a session),
# and a foreground ``git push`` (#1825 — its full pre-push suite blocks
# the loop and the user's queued input). ``manage.py migrate`` is gated
# elsewhere (the ``_BLOCKED_COMMANDS`` t3-CLI redirect); short ``t3 loop
# tick``/``ci``/``doctor`` are NOT slow and are deliberately not listed.
# Read-only git (``status``/``log``/``diff``/``show``/``fetch``) is never
# matched, and a TARGETED ``pytest`` run is exempted in
# :func:`_deny_heavy_main_agent_bash` (the verb still matches here; the
# whole-suite-vs-targeted split is applied at deny time).
_ORCHESTRATOR_HEAVY_BASH_RE = re.compile(
    r"(?:" + _PYTEST_VERB_RE + r"|" + _GIT_PUSH_RE + r"|"
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
# mirroring the ``[skip-skill-gate: <reason>]`` token. An empty reason does not
# unblock.
_FG_OK_RE = re.compile(r"\[fg-ok:\s*\S[^\]]*?\s*\]")


def _call_is_from_subagent(data: dict) -> bool:
    """True when the gated tool call originates from a sub-agent.

    The PreToolUse payload carries a non-empty ``agent_id`` (and
    ``agent_type``) for every sub-agent call and omits it for the main
    agent — the only reliable main-vs-sub-agent signal, because the
    payload's ``transcript_path`` always points at the PARENT session
    transcript (see the #115 root-cause note above). Empty/absent
    ``agent_id`` ⇒ main agent.
    """
    return bool(data.get("agent_id"))


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
    ``Agent`` PreToolUse matcher is wired in ``hooks.json`` (#1646) so a
    foreground Agent dispatch (which fires ``PreToolUse`` with
    ``run_in_background`` in the tool_input) reaches this deny. The gate flipped
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

    (Distinct from the SEPARATE ``Task``/``Workflow`` fan-out vehicle, which
    genuinely bypasses ``PreToolUse`` and fires ``TaskCreated`` — no
    ``run_in_background`` in that schema, so this gate's foreground/background
    signal exists only on the Agent-matcher path, not the TaskCreated one.)

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
    now that an ``Agent`` PreToolUse matcher is wired (#1646). The off-ramps are:
    the kill-switch flag, a sub-agent context, ``run_in_background: true``, and a
    per-call ``[fg-ok: <reason>]`` token in the prompt (mirroring the heavy-Bash
    arm's escape). The deny itself routes through :func:`_fail_open_or_deny`
    (#1692) so the self-rescue allowlist and the master
    ``danger_gate_fail_open`` switch relax it exactly like every other over-deny
    gate — never a bare :func:`emit_pretooluse_deny` lockout.
    """
    if not _orchestrator_boundary_agent_gate_enabled():
        return False
    if _call_is_from_subagent(data) or data.get("tool_input", {}).get("run_in_background") is True:
        return False
    prompt = data.get("tool_input", {}).get("prompt", "")
    if isinstance(prompt, str) and _FG_OK_RE.search(prompt[:512]):
        return False
    return _fail_open_or_deny(
        data,
        "[main-agent-orchestration-guard] Foreground Agent dispatch "
        "DENIED in main agent context.\n"
        "Pass `run_in_background: true` to every Agent invocation "
        "from the main agent, add an explicit `[fg-ok: <reason>]` marker to the "
        "prompt if you truly need a foreground dispatch, or disable this "
        "gate by setting "
        "`[teatree] orchestrator_boundary_agent_gate_enabled = false` in `~/.teatree.toml`.\n"
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
    if _FG_OK_RE.search(command) or not _ORCHESTRATOR_HEAVY_BASH_RE.search(command):
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
        "or — if this is a false positive — set "
        "`orchestrator_bash_gate_enabled = false` under `[teatree]` in "
        "~/.teatree.toml to disable the gate.",
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

    Best-effort read of ``[teatree] orchestrator_turn_budget`` from
    ``~/.teatree.toml``, mirroring :func:`_orchestrator_bash_gate_enabled`'s
    toml-read shape. A missing/broken config keeps the protective default; an
    explicit ``0`` (or any non-positive value) disables the nudge with one
    config line — never a code edit. A non-int value falls back to the default.
    """
    import tomllib  # noqa: PLC0415

    config_path = Path.home() / ".teatree.toml"
    if not config_path.is_file():
        return _DEFAULT_ORCHESTRATOR_TURN_BUDGET
    try:
        with config_path.open("rb") as f:
            config = tomllib.load(f)
    except Exception:  # noqa: BLE001
        return _DEFAULT_ORCHESTRATOR_TURN_BUDGET
    teatree = config.get("teatree") if isinstance(config, dict) else None
    if not isinstance(teatree, dict):
        return _DEFAULT_ORCHESTRATOR_TURN_BUDGET
    raw = teatree.get("orchestrator_turn_budget", _DEFAULT_ORCHESTRATOR_TURN_BUDGET)
    if not isinstance(raw, int) or isinstance(raw, bool):
        return _DEFAULT_ORCHESTRATOR_TURN_BUDGET
    return raw


def _orchestrator_turn_wall_clock_threshold() -> int:
    """Wall-clock responsiveness threshold for the main agent (default 180s; 0 ⇒ off).

    Best-effort read of ``[teatree] orchestrator_turn_wall_clock_seconds`` from
    ``~/.teatree.toml``, mirroring :func:`_orchestrator_turn_budget`'s toml-read
    shape. A missing/broken config keeps the protective default; an explicit
    ``0`` (or any non-positive value) disables the wall-clock dimension with one
    config line. A non-int (or bool) value falls back to the default.
    """
    import tomllib  # noqa: PLC0415

    config_path = Path.home() / ".teatree.toml"
    if not config_path.is_file():
        return _DEFAULT_ORCHESTRATOR_WALL_CLOCK_SECONDS
    try:
        with config_path.open("rb") as f:
            config = tomllib.load(f)
    except Exception:  # noqa: BLE001
        return _DEFAULT_ORCHESTRATOR_WALL_CLOCK_SECONDS
    teatree = config.get("teatree") if isinstance(config, dict) else None
    if not isinstance(teatree, dict):
        return _DEFAULT_ORCHESTRATOR_WALL_CLOCK_SECONDS
    raw = teatree.get("orchestrator_turn_wall_clock_seconds", _DEFAULT_ORCHESTRATOR_WALL_CLOCK_SECONDS)
    if not isinstance(raw, int) or isinstance(raw, bool):
        return _DEFAULT_ORCHESTRATOR_WALL_CLOCK_SECONDS
    return raw


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
    print(json.dumps({"additionalContext": message + _TURN_BUDGET_NUDGE_TAIL}))  # noqa: T201


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

    if len(parts) < 2:  # noqa: PLR2004
        return None
    repo_in_wt = parts[1]
    wt_dir = main_repo_dir / repo_in_wt
    if not (wt_dir / ".git").exists():
        return None
    try:
        branch = subprocess.check_output(  # noqa: S603
            ["git", "-C", str(wt_dir), "--no-optional-locks", "rev-parse", "--abbrev-ref", "HEAD"],  # noqa: S607
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
        from lib.skill_loader import build_trigger_index  # noqa: PLC0415

        from teatree.skill_support.deps import resolve_requires  # noqa: PLC0415

        index = build_trigger_index(_skill_search_dirs())
        return resolve_requires(skills, index)
    except Exception:  # noqa: BLE001
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
            _state_file(session_id, "teatree-active").touch()
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
        _state_file(session_id, "teatree-active").touch()


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
        if len(parts) == 2:  # noqa: PLR2004
            reads[parts[1]] = parts[0]

    # Get current mtime
    try:
        current_mtime = str(Path(file_path).stat().st_mtime)
    except OSError:
        return

    prev_mtime = reads.get(file_path)
    if prev_mtime == current_mtime:
        print(  # noqa: T201
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


# ── PostToolUse: capture Agent-tool sub-agent dispatches ───────────
#
# Issue #778 (reopened): the PreCompact snapshot pinned the loop
# tick-owner (#786 WS3) but NOT ad-hoc background sub-agents an
# orchestrator dispatches via the ``Agent`` tool. The dispatched
# agentId is the handle ``SendMessage`` needs to resume/steer/collect a
# running agent; it lives only in the conversation and is lost on
# auto-compaction, orphaning the agent. Mirror the #970 ``TodoWrite``
# capture: on every ``Agent`` PostToolUse, append the agentId + its
# role/description to ``<session>.agents`` so the snapshot can quote the
# roster back. Each line is ``<agentId>\t<role>`` — append-only, deduped
# on agentId, so a multi-agent fan-out accumulates rather than clobbers.


_AGENT_ID_KEYS = ("agentId", "agent_id", "id")


def _agent_id_from_response(tool_response: object) -> str:
    """Extract the dispatched agentId from an ``Agent`` PostToolUse payload.

    The harness response shape is not contractually fixed, so probe the
    known id-bearing keys on a dict response (``agentId`` / ``agent_id``
    / ``id``). Returns ``""`` when none is present — the caller then
    falls back to scanning the harness tasks dir.
    """
    from typing import cast  # noqa: PLC0415

    if not isinstance(tool_response, dict):
        return ""
    response = cast("dict[str, object]", tool_response)
    for key in _AGENT_ID_KEYS:
        value = response.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _newest_task_agent_id() -> str:
    """Scan the harness tasks output dir for the newest ``a*`` task id.

    Fallback used only when the PostToolUse payload does not expose the
    agentId. The harness writes one ``<agentId>.output`` file per
    dispatched task under ``CLAUDE_TASKS_DIR`` (or
    ``~/.claude/tasks``); the dispatched sub-agent's id is ``a``-prefixed.
    Returns the most-recently-modified match, or ``""`` when the dir is
    absent / has no match. Never raises — capture must never block the
    orchestrator.
    """
    tasks_dir = Path(os.environ.get("CLAUDE_TASKS_DIR", str(Path.home() / ".claude" / "tasks")))
    try:
        candidates = [p for p in tasks_dir.glob("a*.output") if p.is_file()]
    except OSError:
        return ""
    if not candidates:
        return ""
    newest = max(candidates, key=lambda p: p.stat().st_mtime)
    return newest.stem


def handle_track_agents(data: dict) -> None:
    """Persist a dispatched ``Agent`` sub-agent's id + role to ``<session>.agents``.

    No-op for any other tool name. Prefers the agentId carried on the
    PostToolUse ``tool_response`` (``tool_result`` as a secondary
    payload key); falls back to the newest ``a*`` id under the harness
    tasks dir when the payload omits it. Append-only and deduped on
    agentId so a parallel fan-out of sub-agents all survive compaction.
    """
    if data.get("tool_name") != "Agent":
        return
    session_id = data.get("session_id", "")
    if not session_id:
        return

    agent_id = _agent_id_from_response(data.get("tool_response"))
    if not agent_id:
        agent_id = _agent_id_from_response(data.get("tool_result"))
    if not agent_id:
        agent_id = _newest_task_agent_id()
    if not agent_id:
        return

    tool_input = data.get("tool_input", {})
    role = str(tool_input.get("description") or tool_input.get("subagent_type") or "(no description)").strip()

    _ensure_state_dir()
    agents_file = _state_file(session_id, "agents")
    if any(line.split("\t", 1)[0] == agent_id for line in _read_lines(agents_file)):
        return
    _append_line(agents_file, f"{agent_id}\t{role}")


# ── PreCompact: retro-before-compact ──────────────────────────────


def _git_state_for_repo(repo_path: Path) -> dict[str, str] | None:
    """Best-effort current branch / HEAD / dirty / unpushed for *repo_path*.

    Returns ``None`` if *repo_path* is not a git working tree. All subprocess
    calls are short-timeout and exceptions are swallowed — the snapshot must
    never block compaction (#970 / #845 invariant).
    """
    if not (repo_path / ".git").exists():
        return None

    def _git(*args: str) -> str:
        try:
            return subprocess.check_output(  # noqa: S603
                ["git", "-C", str(repo_path), "--no-optional-locks", *args],  # noqa: S607
                text=True,
                timeout=3,
                stderr=subprocess.DEVNULL,
            ).strip()
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return ""

    branch = _git("rev-parse", "--abbrev-ref", "HEAD")
    head = _git("rev-parse", "--short", "HEAD")
    porcelain = _git("status", "--porcelain")
    # ``@{u}`` resolves to the configured upstream; absent ⇒ empty output ⇒ 0.
    unpushed_log = _git("log", "@{u}..HEAD", "--oneline")

    uncommitted_count = len([line for line in porcelain.splitlines() if line.strip()])
    unpushed_count = len([line for line in unpushed_log.splitlines() if line.strip()])
    return {
        "branch": branch or "(detached)",
        "head": head or "(unknown)",
        "uncommitted": str(uncommitted_count),
        "unpushed": str(unpushed_count),
    }


def _open_prs_for_repo(repo_path: Path) -> list[dict]:
    """Return open PRs authored by the current user for *repo_path*.

    Best-effort: a missing ``gh``, no auth, no network, or a non-GitHub
    remote returns ``[]``. Never raises. Tests monkeypatch this symbol
    directly to avoid hitting the network — see
    ``tests/test_pre_compact_snapshot_enriched.py``.
    """
    if not (repo_path / ".git").exists():
        return []
    try:
        out = subprocess.check_output(
            [  # noqa: S607
                "gh",
                "pr",
                "list",
                "--author",
                "@me",
                "--state",
                "open",
                "--limit",
                "20",
                "--json",
                "number,title,headRefName,isDraft",
            ],
            cwd=str(repo_path),
            text=True,
            timeout=3,
            stderr=subprocess.DEVNULL,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return []
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def _resolve_cwd_repo(data: dict) -> Path | None:
    """Resolve the harness-provided ``cwd`` to a directory, if any."""
    cwd = data.get("cwd", "")
    if not cwd:
        return None
    path = Path(cwd)
    return path if path.is_dir() else None


def _render_git_state_section(repo: Path) -> list[str]:
    state = _git_state_for_repo(repo)
    if state is None:
        return []
    return [
        "",
        "## Current git state",
        f"- worktree: `{repo}`",
        f"- branch: `{state['branch']}`",
        f"- HEAD: `{state['head']}`",
        f"- {state['uncommitted']} uncommitted file(s)",
        f"- {state['unpushed']} unpushed commit(s)",
    ]


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
                "This session is the loop-tick OWNER. The loop is tick-driven "
                "(#786 WS3): there is no roster of long-lived sub-agents to "
                "resume — re-arm by ensuring the `t3 loop tick` cron is "
                "registered for this session; each tick atomically claims the "
                "next pending unit via `t3 loop claim-next`."
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

    from teatree.core.management.commands.tasks_session_view import read_harness_todos  # noqa: PLC0415

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

# Skips the ``LoopLease`` DB cross-check (and its ``django.setup()``);
# collapses to the same fail-open value an absent DB already yields.
_SKIP_DB_LEASE_CONSULT_ENV = "T3_LOOP_SKIP_DB_LEASE_CONSULT"


def _db_lease_consult_disabled() -> bool:
    return os.environ.get(_SKIP_DB_LEASE_CONSULT_ENV) == "1"


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
    import fcntl  # noqa: PLC0415

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
        from teatree.utils.singleton import pid_alive  # noqa: F401, PLC0415
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


def _prune_dead_owner(registry: dict[str, dict]) -> dict[str, dict]:
    """Drop registry entries whose recorded owner pid is no longer alive.

    Reuses the existing ``teatree.utils.singleton.pid_alive`` primitive
    rather than re-implementing pid liveness — the locked design calls
    for preferring the existing singleton/pid mechanism. Imported lazily
    to keep this Django-free hook fast on the common path (mirrors the
    lazy ``teatree.skill_support.deps`` import elsewhere in this module).

    Fail-safe (#810): hooks run under whatever interpreter the agent
    harness invokes; ``teatree`` importability is NOT guaranteed there.
    When the import fails we cannot confirm any owner pid is alive, so
    we treat loop ownership as unknown (empty registry) and let the
    caller skip the self-pump rather than crash the session. A ``Stop``
    hook must be crash-proof by contract.
    """
    try:
        from teatree.utils.singleton import pid_alive  # noqa: PLC0415
    except ImportError as exc:
        print(  # noqa: T201 — hook stderr is the module's logging channel
            f"[hook_router] loop self-pump skipped: teatree unavailable ({exc})",
            file=sys.stderr,
        )
        return {}

    return {
        name: entry
        for name, entry in registry.items()
        if isinstance(entry, dict) and pid_alive(int(entry.get("pid", 0) or 0))
    }


def _emit_osc_title() -> None:
    """Best-effort set the terminal tab title for the loop-owner session.

    The interactive-TTY guard IS the openability of the controlling
    terminal: a non-interactive/headless session has no writable tty, so
    the ``open`` fails and the OSC is silently skipped. Never raised.
    """
    with contextlib.suppress(OSError), open(_TTY_PATH, "a", encoding="utf-8") as tty:  # noqa: PTH123
        tty.write("\033]0;TEATREE LOOP\007")


# #786 WS3: the per-loop spawn-brief machinery (_LOOP_SPAWN_BRIEFS /
# _loop_spawn_briefs / _brief_block / _DURABILITY_NOTE) is RETIRED — there
# is no immortal roster to re-spawn from a brief. The loop is the
# `t3 loop tick` cron + WS1 atomic claim-next + WS2 LoopLease; surviving
# an owner death is "the next session becomes tick-owner and keeps
# ticking", not "re-spawn N sub-agents from persisted briefs".


def _now_ts() -> int:
    import time  # noqa: PLC0415

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
# driven by the machine-wide ``t3 loop tick`` cron (#676): each tick the
# loop-owner session atomically claims pending DB work (WS1
# ``t3 loop claim-next`` — conditional-UPDATE CAS) and spawns a FRESH,
# BOUNDED sub-agent for just that unit, which returns. Statelessness
# across ticks IS the compaction-proofing — a worker dying mid-task
# leaves its Task reclaimable; the next tick re-dispatches it. The
# loop-tick *executor* mutex is the WS2 ``LoopLease`` row; this
# Django-free hook registry only records which *session* is the
# tick-owner (one record, never a roster) so the #758/#810 Stop-hook
# self-pump can gate on it without a Django bootstrap in the hot path.

_TICK_DISPATCH_OWNER_DIRECTIVE = (
    "TEATREE LOOP — tick-driven, no roster to spawn.\n\n"
    "This session is the teatree loop-tick OWNER. The loop is NOT a set of "
    "long-lived sub-agents you spawn or keep alive: it is the recurring "
    "`t3 loop tick` cron. Each tick, claim the next pending unit atomically "
    "with `t3 loop claim-next` and spawn ONE fresh, bounded sub-agent for "
    "just that unit (it does the work and returns). No persistent loop "
    "roster, nothing to re-spawn on compaction — a worker dying mid-task "
    "leaves its Task reclaimable and the next tick re-dispatches it. Ensure "
    "the `t3 loop tick` cron is registered for this session." + _RENAME_REMINDER
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
    "UI (and reconnect the Claude-in-Chrome extension per /t3:e2e) before "
    "relying on any outbound message."
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
    "TEATREE LOOP — tick-driven; another session owns the tick.\n\n"
    "Another live session is the teatree loop-tick owner (owner session "
    "{owner_session}). Do NOT arm a competing `t3 loop tick` cron and do "
    "NOT spawn loop sub-agents. The loop-owner gate (#1073) is now a HARD "
    "gate: a non-owner `t3 loop tick` will SKIP before any scanner / Slack "
    "DM-drain / dispatch runs at all — it does NOT execute the tick. "
    "Stay idle with respect to the loop. (If you ARE the user's main "
    "session and a foreign session has hijacked the loop, run `t3 loop "
    "claim --take-over` and the hijacker's next tick SKIPs within one tick.)"
)


def _tick_owner_record(session_id: str, agent_id: str) -> dict[str, dict]:
    """Single owner-session record under ``_OWNER_LOOP`` (no roster, #786 WS3).

    The hook layer only needs *which session* is the tick-owner so the
    Stop-hook self-pump (#758/#810) can gate on it Django-free. The
    immortal-roster fields (per-loop ``spawn_brief``) are retired — there
    is nothing to re-spawn. The owner pid is ``os.getppid()`` (the
    long-lived session process, not this ephemeral hook subprocess).
    """
    return {
        _OWNER_LOOP: {
            "session_id": session_id,
            "agent_id": agent_id,
            "pid": os.getppid(),
            "heartbeat_ts": _now_ts(),
        }
    }


def _live_lease_is_foreign(stored_pid: int, current_pid: int | None) -> bool:
    """Return True iff a LIVE foreign-session lease should be treated as genuinely foreign.

    Called only for live leases whose ``session_id`` differs from the current session.
    Returns False (evictable) when stored_pid matches current_pid (post-compaction
    same-process self-reclaim) or pid_alive confirms the owner process is dead.
    Returns True (KEEP) when pid_alive is unavailable (conservative bias, INV4) or
    the owner process is still alive and belongs to a different OS process (INV1).
    """
    if current_pid is not None and stored_pid == current_pid:
        return False
    try:
        from teatree.utils.singleton import pid_alive  # noqa: PLC0415
    except ImportError:
        return True
    else:
        return pid_alive(stored_pid)


def _db_live_foreign_owner(session_id: str, current_pid: int | None) -> str:
    """Return the session id of a genuinely LIVE foreign ``loop-owner`` DB lease, or ``""``.

    #1604: called when the file registry has no entry for the tick-owner
    (empty after prune / fail-safe) to detect registry/DB desync. If the
    DB shows a live claim by a *different* session that is also a
    *different alive process*, that session is still the rightful owner —
    the new session must stay idle (INV1). Fails open (returns ``""``) on
    any DB/import error so a hiccup never blocks the SessionStart directive.
    """
    if _db_lease_consult_disabled():
        return ""
    if not bootstrap_teatree_django():
        return ""
    try:
        import datetime  # noqa: PLC0415

        from teatree.core.models import LoopLease  # noqa: PLC0415
        from teatree.utils.singleton import pid_alive  # noqa: PLC0415

        row = LoopLease.objects.filter(name="loop-owner").values("session_id", "owner_pid", "lease_expires_at").first()
        owner_session = (row or {}).get("session_id") or ""
        is_foreign_session = bool(owner_session) and owner_session != session_id
        expires_at = (row or {}).get("lease_expires_at")
        stored_pid = (row or {}).get("owner_pid")
        # Liveness is pid-anchored: an alive owner_pid is a live owner past
        # its tick TTL (the busy-owner hijack the TTL-only check missed).
        is_live = (expires_at is not None and expires_at > datetime.datetime.now(tz=datetime.UTC)) or (
            stored_pid is not None and pid_alive(stored_pid)
        )
        pid_is_foreign = stored_pid is None or _live_lease_is_foreign(stored_pid, current_pid)
    except Exception:  # noqa: BLE001
        return ""
    else:
        return owner_session if (is_foreign_session and is_live and pid_is_foreign) else ""


def _evict_stale_db_lease_owner(session_id: str, current_pid: int | None) -> None:
    """Conditionally evict the ``LoopLease`` ``loop-owner`` row (#1604).

    #1380 (#1107 follow-up). Context compaction rotates the Claude
    ``session_id``. The file registry's ``t3-loop-tick-owner`` slot is
    rewritten to the new id, but the DB ``LoopLease`` row name=
    ``loop-owner`` still carries the OLD id with an unexpired
    ``lease_expires_at``. ``CLAUDE_SESSION_ID`` is empty in Bash-tool
    subprocesses (#1107) so the next ``t3 loop tick`` resolves the NEW
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
        from teatree.core.models import LoopLease  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return
    try:
        LoopLease.objects.evict_stale_owner("loop-owner", keep_session_id=session_id, current_pid=current_pid)
    except Exception:  # noqa: BLE001
        return


def _claim_session_handover(session_id: str) -> str | None:
    """Claim an unclaimed session hand-off for *session_id*, or ``None``.

    The zero-copy-paste takeover: a fresh / non-owner session picks up a
    hand-off targeted AT it or parked for "next session" from the
    ``SessionHandover`` DB table (the source of truth), marks it claimed so
    it injects exactly once, and returns its payload to merge into the
    SessionStart ``additionalContext``. Falls back to the XDG file mirror
    when the DB is unreachable (a brand-new session whose process predates
    a readable DB). Best-effort: any Django/DB error fails open to the file
    fallback, then to ``None`` — a hand-off pickup must never block the
    SessionStart directive.
    """
    payload = ""
    from_session = ""
    if bootstrap_teatree_django():
        try:
            from teatree.core.models import SessionHandover  # noqa: PLC0415

            claimed = SessionHandover.objects.claim_next(session_id)
            if claimed is not None:
                payload = claimed.payload
                from_session = claimed.from_session
        except Exception:  # noqa: BLE001 — never block SessionStart on a DB hiccup
            payload = ""

    if not payload:
        payload, from_session = _claim_session_handover_from_file()
    if not payload:
        return None
    origin = f" from session `{from_session}`" if from_session else ""
    return (
        f"SESSION HAND-OFF RECEIVED{origin} — another session handed its full "
        "in-flight work to you. Read the durable-state snapshot below, then "
        "resume that work (re-derive identity, worktrees, open PRs, and the "
        "next action):\n\n" + payload
    )


def _claim_session_handover_from_file() -> tuple[str, str]:
    """Read the XDG mirror as a one-shot hand-off fallback, renaming it on claim.

    Returns ``(payload, from_session)`` or ``("", "")``. The mirror is the
    bootstrap path for a brand-new session that cannot reach the DB. To keep
    the file single-use (mirroring the DB ``claimed_at`` once-only contract)
    the claimed file is renamed to ``latest.claimed.md`` so a re-fired
    SessionStart does not re-inject it.
    """
    src_dir = Path(__file__).resolve().parents[2] / "src"
    added = False
    try:
        if str(src_dir) not in sys.path:
            sys.path.insert(0, str(src_dir))
            added = True
        from teatree.config import load_config  # noqa: PLC0415

        path = load_config().user.handover_mirror_path
        text = path.read_text(encoding="utf-8").strip() if path.is_file() else ""
        if not text:
            return "", ""
        with contextlib.suppress(OSError):
            path.replace(path.with_name("latest.claimed.md"))
    except Exception:  # noqa: BLE001
        return "", ""
    else:
        return text, ""
    finally:
        if added:
            with contextlib.suppress(ValueError):
                sys.path.remove(str(src_dir))


def _autocompact_kill_switch_advisory() -> str | None:
    """Return the #980 advisory text when the harness kill-switch trips.

    The Claude Code harness silently disables auto-compaction on
    1M-capable models (currently claude-opus-4-7) unless an
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
        from teatree.core.autocompact_advisory import AutocompactConfig, advisory_text  # noqa: PLC0415

        return advisory_text(AutocompactConfig.from_env())
    except Exception:  # noqa: BLE001
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
        from teatree.core.account_fingerprint import fingerprint_switched  # noqa: PLC0415

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
    so the SessionStart hot path stays inside its 3s budget: the live
    ``claude mcp list`` probe would blow it, so session start only nudges the
    agent to run ``t3 doctor check`` (which does the bounded probe) when there
    is something to verify. Any import / read failure returns None so the
    directive never blocks SessionStart.
    """
    src_dir = Path(__file__).resolve().parents[2] / "src"
    added = False
    try:
        if str(src_dir) not in sys.path:
            sys.path.insert(0, str(src_dir))
            added = True
        from teatree.core.mcp_connectivity import has_enabled_mcp_servers  # noqa: PLC0415

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

    advisory = _autocompact_kill_switch_advisory()
    if advisory:
        context = f"{context}\n\n---\n\n{advisory}"

    switch_advisory = _account_switch_advisory()
    if switch_advisory:
        context = f"{switch_advisory}\n\n---\n\n{context}"

    mcp_advisory = _mcp_connectivity_advisory()
    if mcp_advisory:
        context = f"{mcp_advisory}\n\n---\n\n{context}"
    return context


def handle_session_start_bootstrap(data: dict) -> None:
    """Emit the tick-dispatch bootstrap directive (#786 WS3 — roster retired).

    The immortal-singleton roster (spawn/takeover/resume/re-attach a fixed
    set of long-lived loop sub-agents) is GONE. The loop is the
    ``t3 loop tick`` cron + WS1 atomic ``claim-next`` + WS2 ``LoopLease``
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
    """
    session_id = data.get("session_id", "")
    if not session_id:
        return
    if not _loop_auto_load_active(session_id):
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

    # #1380 / #1604: conditionally evict any stale DB ``loop-owner`` row.
    # Runs when the registry had no entry (fresh machine or dead-owner prune)
    # and the DB also showed no live foreign lease, OR (#1838 PR#7a) on a
    # compaction resume — the eviction ORPHANS the stale lease (``session_id=""``)
    # synchronously before any tick, so the lead's next ``t3 loop tick``
    # re-anchors ``loop-owner`` uncontested and no maker pane can win the
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
    source = data.get("source", "")
    if became_owner_after_rotation or source == "compact":
        _evict_stale_db_lease_owner(session_id, current_pid=current_pid)

    # OSC write is a tty side effect, not registry state — keep it out of
    # the flock critical section.
    if emit_osc:
        _emit_osc_title()

    context = _merge_session_start_context(context, session_id, source)

    # #1452: the harness silently drops the legacy flat top-level
    # ``{"additionalContext": ...}`` form for SessionStart events; the
    # documented schema (Agent SDK ``SessionStartHookSpecificOutput``)
    # requires the nested envelope. Confirmed empirically: 24 compactions
    # in session a1e3d2d8-… emitted the flat form and zero of them
    # injected the snapshot text into the post-compact model context.
    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": context,
            },
        },
        sys.stdout,
    )


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
        result = subprocess.run(  # noqa: S603
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

    The single signal both the loop-registration nudge and the inline-question
    Stop gate share to decide "is this an autonomous/loop-driven turn vs an
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
    import time  # noqa: PLC0415

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


def _bash_env_file() -> Path:
    """Path to the shell-sourceable teatree env file (``~/.teatree``).

    The harness spawns the Stop hook as a bare ``python3`` that does NOT
    source the user's shell profile, so ``export VAR=value`` lines in this
    file never reach ``os.environ`` (the ``ensure-skills-loaded.sh``
    bootstrap calls it out: "hooks don't source .zshrc/.teatree").
    ``TEATREE_BASH_ENV_FILE`` overrides the location (tests / non-default
    HOME).
    """
    override = os.environ.get("TEATREE_BASH_ENV_FILE", "").strip()
    if override:
        return Path(override)
    return Path(os.environ.get("HOME", str(Path.home()))) / ".teatree"


def _read_bash_env_var(name: str) -> str:
    """Last ``export <name>=<value>`` value in :func:`_bash_env_file`.

    Pure-stdlib parse — no ``teatree`` import (the hook interpreter may
    lack it, #810) and no shell invocation. Tolerant of a leading
    ``export``/whitespace, spaces around ``=``, single/double quotes, and
    trailing ``# comments``. Crash-proof: a missing or unreadable file
    yields ``""``. Last assignment wins, mirroring shell sourcing.
    """
    try:
        path = _bash_env_file()
        if not path.is_file():
            return ""
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    value = ""
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, sep, rest = line.partition("=")
        if not sep or key.strip() != name:
            continue
        value = _strip_bash_value(rest)
    return value


def _strip_bash_value(rest: str) -> str:
    """Strip surrounding quotes and a trailing ``# comment``."""
    rest = rest.strip()
    quote = rest[0] if rest[:1] in {"'", '"'} else ""
    if quote:
        end = rest.find(quote, 1)
        if end != -1:
            return rest[1:end]
        return rest[1:]
    return rest.split("#", 1)[0].strip()


def _resolve_loop_env(name: str) -> str:
    """Resolve a loop control var: process env first, bash env file second.

    The process env is authoritative — an explicit value (even empty)
    there is never overridden by the file. The file is consulted only when
    the var is wholly absent from ``os.environ``, recovering the
    kill-switch the unsourced Stop hook would otherwise miss.
    """
    if name in os.environ:
        return os.environ[name]
    return _read_bash_env_var(name)


def _all_loops_disabled() -> bool:
    """Does ``T3_LOOPS_DISABLED`` fully prune the loop for this session?

    Mirrors the ``all`` sentinel of ``teatree.loops.config._env_disabled_names``
    without importing ``teatree`` (a Stop hook runs under whatever
    interpreter the harness invokes, where ``teatree`` may be absent —
    #810). The orchestrator gates every ``t3 loop tick`` job through that
    same env var; the Stop self-pump is the in-session counterpart of a
    tick, so it must honour the kill-switch identically. ``T3_LOOPS_DISABLED=all``
    means every non-always-on loop is off — re-pumping then only re-runs the
    one ``always_on`` ``dispatch`` scanner, doing no useful work while the
    pending Tasks that drive ``pending-spawn`` keep the pump re-arming every
    interval. That is the busy-loop the prune is meant to silence, so a fully
    pruned session never pumps.

    The value is resolved via :func:`_resolve_loop_env` so the kill-switch
    set in ``~/.teatree`` (never sourced into the bare-``python3`` Stop
    hook) is still honoured.
    """
    raw = _resolve_loop_env("T3_LOOPS_DISABLED").strip()
    if not raw:
        return False
    return any(part.strip().lower() == "all" for part in raw.split(","))


def _pause_suppresses_self_pump() -> bool:
    """True when an explicit user pause must win over the standing loop directive.

    The self-pump is teatree's own re-firing Stop directive (#2247/#2250): it
    re-emits ``{"decision": "block", ...}`` to resume the loop every turn while
    consolidated work remains. When the user has explicitly paused —
    availability resolves to ``away`` via the same ``resolve_mode`` chain the
    AskUserQuestion deferral honours (a manual ``t3 teatree availability away``
    override, or an out-of-window schedule) — that nag must SUPPRESS, so an
    explicit pause wins over the goal exactly as it does for the agent.

    FAIL SAFE — suppress on indeterminate: ``_resolved_away_mode`` already
    collapses a missing/unimportable ``teatree`` or an availability-read error
    to ``False`` ("not away"), which here would mean "keep pumping". That is the
    UNSAFE direction for a Stop hook: an indeterminate signal must allow the
    stop (suppress the nag), never loop through a possible pause. So this
    predicate treats away AND any availability-resolution error as suppress; it
    pumps ONLY when availability resolves cleanly to ``present``.
    """
    try:
        return _resolved_away_mode()
    except Exception:  # noqa: BLE001 — indeterminate ⇒ suppress (allow stop, never nag through a pause)
        return True


def _self_pump_suppressed(session_id: str) -> bool:
    """Is the Stop self-pump gated off for this session (#959)?

    The self-pump is a SINGLETON bound to the ONE designated loop-owner
    session (the ``_OWNER_LOOP`` record — set at SessionStart, released
    at SessionEnd, transferable across sessions). WS4's "per-agent,
    decoupled from the tick-owner" model leaked the loop into EVERY
    fresh/unrelated session — a brand-new blog-writing session
    immediately started pumping ``t3 loop tick``/``claim-next`` and
    spawning review sub-agents. This gate is checked FIRST so a
    non-owner session's Stop hook is a clean no-op: no ``pending-spawn``
    subprocess, no registry write, no error noise in the transcript. The
    per-agent consolidation slot stays as a secondary cross-session
    dedup, NOT a substitute for this gate.

    ``T3_LOOPS_DISABLED=all`` fully prunes the loop (the same kill-switch
    the orchestrator honours per tick job): the owner's Stop hook becomes
    a clean no-op so a pruned environment cannot busy-loop on stale
    pending work. Both this and ``T3_LOOP_DISOWN`` resolve through
    :func:`_resolve_loop_env`, which falls back to the ``~/.teatree`` bash
    env file when the var is absent from the process env — the bare
    ``python3`` Stop hook never sources that file, so a kill-switch set
    only there would otherwise be invisible to the self-pump.

    Immediate mitigation knob: ``T3_LOOP_DISOWN`` truthy (in the session's
    env or the bash env file) makes even the owner's Stop hook a clean
    no-op, so a session can stop driving the loop in-process without
    touching the registry or ending the session.

    A user pause (away, #2247/#2250, :func:`_pause_suppresses_self_pump`) or a
    durable DB ``LoopState`` pause of ``dispatch`` (#1913, :func:`db_loop_state_suppresses_self_pump`) gate it off.
    """
    if _all_loops_disabled() or db_loop_state_suppresses_self_pump():
        return True
    if _resolve_loop_env("T3_LOOP_DISOWN").strip() not in _DISOWN_FALSEY:
        return True
    if _pause_suppresses_self_pump():
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
        f"without waiting for an external prompt. Run `T3_LOOP_SESSION_ID={session_id} "
        f"T3_LOOP_SESSION_PID={session_pid} "
        "t3 loop tick`, then "
        "repeatedly `t3 loop claim-next` and spawn ONE fresh, bounded sub-agent "
        "(Agent tool) for each claimed unit until it returns nothing — the "
        "claim is atomic (#786 WS1), so no separate post-spawn claim step and "
        "no double-dispatch. Outstanding now:\n" + _format_pending_summary(pending)
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


def _read_transcript_entries(transcript_path: str) -> list[dict]:
    """Parse the Claude Code transcript JSONL into a list of dict entries.

    Fail-safe: an empty/missing/unreadable file or malformed lines yield
    ``[]`` (the caller then does nothing) rather than raising.
    """
    if not transcript_path:
        return []
    path = Path(transcript_path)
    if not path.is_file():
        return []
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return []
    entries: list[dict] = []
    for raw_line in raw.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            entries.append(parsed)
    return entries


def _entry_role(entry: dict) -> str | None:
    message = entry.get("message")
    if isinstance(message, dict):
        return message.get("role")
    return entry.get("type")


def _entry_content(entry: dict) -> list:
    message = entry.get("message")
    content = message.get("content", []) if isinstance(message, dict) else []
    return content if isinstance(content, list) else []


def _last_assistant_turn(transcript_path: str) -> tuple[str, bool] | None:
    """Return ``(final_assistant_text, used_question_tool)`` for the last turn.

    The "last turn" is every assistant message after the most recent user
    message in the transcript JSONL. ``final_assistant_text`` is the
    concatenated text blocks of those messages; ``used_question_tool`` is
    ``True`` if any ``AskUserQuestion`` ``tool_use`` block appears in the
    turn. Returns ``None`` when the transcript is missing, unreadable,
    empty, or has no trailing assistant turn (fail-safe to "do nothing").
    """
    texts: list[str] = []
    used_tool = False
    for entry in reversed(_read_transcript_entries(transcript_path)):
        role = _entry_role(entry)
        if role == "user":
            break
        if role != "assistant":
            continue
        for block in _entry_content(entry):
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                texts.append(str(block.get("text", "")))
            elif block.get("type") == "tool_use" and block.get("name") == "AskUserQuestion":
                used_tool = True
    if not texts:
        return None
    # entries were walked newest→oldest; restore reading order
    return "\n".join(reversed(texts)), used_tool


_STRUCTURED_QUESTION_BLOCK = (
    "TEATREE GATE — a user-directed question was asked inline in prose with no "
    "AskUserQuestion tool call in this turn. Inline questions are invisible in "
    "autonomous/loop runs (they read as log lines) so the decision is lost. "
    "Re-ask the SAME question through the AskUserQuestion tool now — one question "
    "at a time, with concrete options — then continue. This is a non-bypassable "
    "gate (no `relax:` escape): the question must go through the structured tool."
)


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

    Context-aware: this gate exists because an inline question is invisible in
    an autonomous/loop run (it reads as a log line, so the decision is lost). In
    an attended interactive session a human IS reading the prose, so the gate is
    pointless nagging. It therefore only enforces on a loop-driven turn —
    ``_session_drives_loop`` is the same signal the loop-registration gate uses
    (this session owns the tick, OR there is no live owner). When a *different*
    live session owns the loop, this is the attended session and the gate is
    skipped. DEGRADATION CONTRACT — FAIL SAFE: an unknown/unreadable ownership
    signal yields a driver verdict (see ``_session_drives_loop``), so the gate
    keeps firing.
    """
    if data.get("stop_hook_active"):
        return None
    if not _session_drives_loop(data.get("session_id", "")):
        return None
    turn = _last_assistant_turn(data.get("transcript_path", ""))
    if turn is None:
        return None
    text, used_question_tool = turn
    if used_question_tool or not is_user_directed_question(text):
        return None
    if _is_classifier_relax_explanation(text):
        return None
    json.dump({"decision": "block", "reason": _STRUCTURED_QUESTION_BLOCK}, sys.stdout)
    return True


# ── Classifier-relax PreToolUse allow (sanctioned denial protocol) ──────────
#
# Threat model:
#
#   WHAT THIS ALLOWS: Edit/Write to ~/.claude/settings.json ONLY when there is
#   transcript evidence of the exact Step-3 user approval from the sanctioned
#   classifier-relax flow.  The evidence requires both a specific AskUserQuestion
#   option text ("Allow it (relax classifier)") AND an affirmative user response.
#
#   PER-WRITE / CONSUME-ONCE CONSENT (review Finding 2): an approval authorises
#   exactly ONE subsequent settings.json write — the next one — not every later
#   write in the session.  The scan walks the transcript from the END and binds
#   to the MOST RECENT approval pair (AskUserQuestion-with-relax-option →
#   affirmative user turn).  It then verifies that NO settings.json Edit/Write
#   tool_use has occurred AFTER that approval.  If a settings.json write already
#   happened since the approval, the consent is spent: the pending write is a
#   replay of consumed consent and is DENIED.  A genuine second escalation
#   (a fresh AskUserQuestion-relax + fresh affirmative, with no settings write
#   since) re-authorises exactly the next write — consume-once is per-approval,
#   not once-per-session-forever.
#
#   WHAT THIS DOES NOT ALLOW:
#   - Any other target path (not settings.json).
#   - Edit/Write without transcript evidence of the Step-3 approval.
#   - Replay of a consumed approval: an approval whose settings.json write has
#     already completed cannot authorise a second, later, unrelated write.
#   - Approving "do X without running AskUserQuestion first" — the AskUserQuestion
#     tool call itself must appear in the same session transcript.
#
#   RESIDUAL RISK: A transcript file the agent can write could in theory be
#   crafted to contain a forged AskUserQuestion block.  This is mitigated by:
#   (a) the hook path and the transcript are both controlled by the harness, not
#   the agent, in normal operation; (b) the sanctioned flow is narrow and
#   well-documented so detection patterns are specific; (c) the allow emitted is
#   only for settings.json, not arbitrary paths; (d) consume-once binds the
#   allow to the next write after the most-recent approval, so a single forged
#   or genuine approval cannot be amortised across many settings writes.
#
#   TRACKED RESIDUAL (#857): the hook authorises *that* a settings.json write
#   occurs (sanctioned by the user's explicit approval) but does NOT
#   schema-validate the write *payload*.  Optional content-shape validation
#   (only permit appending a string entry to permissions.allow/autoMode.allow)
#   is tracked as a follow-up hardening in issue #857; it is intentionally out
#   of scope here because the user explicitly approved the write.

_CLASSIFIER_RELAX_OPTION = "Allow it (relax classifier)"

# Affirmative selection of the relax option (review Findings 3/4).  Precise,
# not loosely spoofable: it matches an explicit selection of the option label
# / "allow it" intent or a clear standalone yes — NOT a bare "relax" substring
# (which false-matched "please relax the check") and NOT only a start-anchored
# "yes" (which over-denied "Actually, yes — go ahead").  Deliberately excludes
# loose verbs like "do it" because the DECLINE option label is "Keep the
# denial (do it differently)" — a substring match there would invert consent.
# Word boundaries keep it from matching inside unrelated words.
_CLASSIFIER_RELAX_AFFIRMATIVE = re.compile(
    r"allow it(?:\s*\(relax classifier\))?"  # the option label / "allow it"
    r"|relax classifier"  # explicit protocol shorthand, not bare "relax"
    r"|\byes\b"  # a clear yes anywhere (word-bounded)
    r"|\b(?:go ahead|approve|approved|affirmative|confirm|confirmed)\b",
    re.IGNORECASE,
)


# Module-level constant for the (unexpanded) settings path — single source of
# truth, no per-call literal.  Expansion stays in _settings_json_target() and
# is performed at call time (NOT memoised at import) so the HOME env var that
# conftest._isolate_env monkeypatches per-test is respected.
_SETTINGS_JSON_PATH = "~/.claude/settings.json"


def _settings_json_target() -> str:
    """Resolved absolute path of ``_SETTINGS_JSON_PATH`` (HOME-sensitive).

    Expanded at call time (not module import) so the HOME env var used during
    tests (monkeypatched by conftest._isolate_env) is respected.
    """
    return str(Path(_SETTINGS_JSON_PATH).expanduser())


def _block_is_settings_write(block: dict) -> bool:
    """True when ``block`` is an Edit/Write tool_use targeting settings.json.

    Callers must pre-filter with ``isinstance(block, dict)`` (mirrors the
    ``_ask_question_has_relax_option`` contract and the call sites below).
    """
    if block.get("type") != "tool_use":
        return False
    if block.get("name") != "Edit" and block.get("name") != "Write":
        return False
    tool_input = block.get("input")
    raw_path = tool_input.get("file_path", "") if isinstance(tool_input, dict) else ""
    try:
        return str(Path(str(raw_path)).expanduser()) == _settings_json_target()
    except (OSError, ValueError, RuntimeError):
        return False


def _ask_question_has_relax_option(block: dict) -> bool:
    """True when an ``AskUserQuestion`` tool_use offers the verbatim relax option.

    Iterates the structured option labels and matches the exact (whitespace-
    normalised) option text — not a repr substring of the options list
    (review Finding 5).
    """
    if block.get("type") != "tool_use" or block.get("name") != "AskUserQuestion":
        return False
    tool_input = block.get("input")
    questions = tool_input.get("questions", []) if isinstance(tool_input, dict) else []
    if not isinstance(questions, list):
        return False
    target = " ".join(_CLASSIFIER_RELAX_OPTION.split())
    for question in questions:
        if not isinstance(question, dict):
            continue
        options = question.get("options", [])
        if not isinstance(options, list):
            continue
        for option in options:
            label = option.get("label", option) if isinstance(option, dict) else option
            if isinstance(label, str) and " ".join(label.split()) == target:
                return True
    return False


def _user_entry_affirms_relax(entry: dict) -> bool:
    """True when a user transcript ``entry`` affirmatively selects the relax option."""
    texts = [str(b.get("text", "")) for b in _entry_content(entry) if isinstance(b, dict) and b.get("type") == "text"]
    return bool(_CLASSIFIER_RELAX_AFFIRMATIVE.search(" ".join(texts).strip()))


def _has_sanctioned_relax_approval(transcript_path: str) -> bool:
    """Return True only for an unconsumed, most-recent Step-3 relax approval.

    Algorithm (review Finding 2 — per-write / consume-once consent).
    Step one: walk the transcript from the END to find the MOST RECENT
    assistant ``AskUserQuestion`` tool_use that offers the verbatim relax
    option.  Step two: from that point forward, find the FIRST subsequent
    user turn; the approval holds only if that turn affirmatively selects
    the relax option (interleaved non-user entries are skipped).  Step
    three (consume-once): scan every entry AFTER that approving user turn;
    if a settings.json Edit/Write tool_use already occurred, the consent is
    spent — the pending write would be a replay — so return False.

    Returns False on any failure (missing transcript, no matching turn, no
    affirmative response, no subsequent user turn, consent already consumed)
    — fail-safe to "no allow".
    """
    entries = _read_transcript_entries(transcript_path)
    for idx in range(len(entries) - 1, -1, -1):
        entry = entries[idx]
        if _entry_role(entry) != "assistant":
            continue
        if not any(
            isinstance(block, dict) and _ask_question_has_relax_option(block) for block in _entry_content(entry)
        ):
            continue
        # Most-recent relax AskUserQuestion is at index ``idx``.  Find the
        # first user turn after it and require an affirmative selection.
        approval_user_idx: int | None = None
        for j in range(idx + 1, len(entries)):
            if _entry_role(entries[j]) != "user":
                continue
            if not _user_entry_affirms_relax(entries[j]):
                return False
            approval_user_idx = j
            break
        if approval_user_idx is None:
            # AskUserQuestion-relax with no subsequent user turn => not approved.
            return False
        # Consume-once: a settings.json write already performed since the
        # approving turn spends the consent — deny the replay.
        for k in range(approval_user_idx + 1, len(entries)):
            if any(isinstance(block, dict) and _block_is_settings_write(block) for block in _entry_content(entries[k])):
                return False
        return True
    return False


def handle_allow_classifier_relax_settings_write(data: dict) -> bool | None:
    """Allow Edit/Write to ~/.claude/settings.json after sanctioned Step-3 approval.

    Emits ``{"permissionDecision": "allow"}`` and returns ``True`` ONLY when:
    1. The tool being called is ``Edit`` or ``Write``.
    2. The target file path resolves to ``~/.claude/settings.json``.
    3. The transcript contains ``AskUserQuestion`` with the relax option
        AND an affirmative user response (Step-3 approval from the protocol).

    Any condition failing returns ``None`` without emitting anything — all
    subsequent handlers including any deny handler remain in play.

    This handler must be registered FIRST in the PreToolUse chain so it fires
    before any deny handler that might block the settings.json write.

    See the threat model in the module-level comment block above.
    """
    if data.get("tool_name") not in {"Edit", "Write"}:
        return None
    tool_input = data.get("tool_input") or {}
    raw_path = tool_input.get("file_path", "")
    if str(Path(str(raw_path)).expanduser()) != _settings_json_target():
        return None
    if not _has_sanctioned_relax_approval(data.get("transcript_path", "")):
        return None
    json.dump({"permissionDecision": "allow"}, sys.stdout)
    return True


_SESSION_END_ORPHAN_TIMEOUT = 4
_SESSION_END_ORPHAN_PREVIEW = 5


def _fetch_orphans() -> list[dict]:
    """Invoke ``t3 teatree workspace list-orphans`` and return its JSON, or ``[]``."""
    t3_bin = shutil.which("t3")
    if not t3_bin:
        return []
    try:
        result = subprocess.run(  # noqa: S603
            [t3_bin, "teatree", "workspace", "list-orphans"],
            capture_output=True,
            text=True,
            timeout=_SESSION_END_ORPHAN_TIMEOUT,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return []
    if result.returncode != 0 or not result.stdout.strip():
        return []
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def _format_orphan_summary(orphans: list[dict]) -> str:
    """Return a one-line-per-orphan bullet list, truncated to _SESSION_END_ORPHAN_PREVIEW entries."""
    preview = orphans[:_SESSION_END_ORPHAN_PREVIEW]
    lines = [f"  - {o.get('repo', '?')} ({o.get('branch', '?')}, {o.get('ahead_count', 0)} ahead)" for o in preview]
    if len(orphans) > _SESSION_END_ORPHAN_PREVIEW:
        lines.append(f"  - …and {len(orphans) - _SESSION_END_ORPHAN_PREVIEW} more")
    return "\n".join(lines)


def handle_session_end(data: dict) -> None:
    """Suggest retro and surface orphan branches at session close."""
    session_id = data.get("session_id", "")
    if not session_id:
        return

    skills_file = STATE_DIR / f"{session_id}.skills"
    loaded: set[str] = set()
    if skills_file.is_file():
        loaded = {line.strip() for line in skills_file.read_text(encoding="utf-8").splitlines() if line.strip()}

    lifecycle_skills = {"t3:code", "t3:debug", "t3:test", "t3:ship", "t3:review", "t3:ticket"}
    retro_relevant = bool(loaded & lifecycle_skills)

    orphans = _fetch_orphans() if retro_relevant else []

    if not retro_relevant and not orphans:
        return

    parts: list[str] = []
    if retro_relevant:
        parts.append(
            "SESSION ENDING — lifecycle skills were loaded during this session "
            f"({', '.join(sorted(loaded & lifecycle_skills))}). "
            "Consider running /t3:retro to capture learnings before the session ends.",
        )
    if orphans:
        parts.append(
            f"ORPHAN BRANCHES DETECTED ({len(orphans)}) — branches with local work and no open PR:\n"
            f"{_format_orphan_summary(orphans)}\n"
            "Run `t3 teatree pr ensure-pr --branch <name>` to track them, "
            "or `t3 teatree workspace clean-all` to reap synced ones.",
        )

    json.dump({"additionalContext": "\n\n".join(parts)}, sys.stdout)


# ── PostToolUse: track-cron-jobs ──────────────────────────────────────


_LOOP_NAME_MAX = 20


def _clean_token(token: str) -> str:
    """Strip surrounding/trailing punctuation and backticks from a token."""
    return token.strip("`").strip(".,;:!?\"'()[]{}/").strip("`")


def _derive_loop_name(prompt: str) -> str:
    """Derive a short display name from a cron/loop prompt.

    - The canonical teatree loop prompt maps to a stable readable name.
    - Slash-command prompts use the command token.
    - Otherwise a short label is taken from the first meaningful word.

    Surrounding punctuation and backticks are always stripped.
    """
    prompt = prompt.strip()

    # 1. A teatree loop-tick prompt → a stable readable name: a per-loop tick
    # (#2650) shows that loop's OWN name (the native `/loop` it drives), the
    # legacy fat-tick prompt shows "tick".
    per_loop = loop_name_from_prompt(prompt)
    if per_loop is not None or prompt == _LOOP_PROMPT or prompt.startswith(_LOOP_PROMPT):
        return (per_loop or "tick")[:_LOOP_NAME_MAX]

    if prompt.startswith("!"):
        prompt = prompt[1:].strip()

    parts = prompt.split()
    if not parts:
        return "loop"

    # `t3 loop <subcommand>` shell form → the subcommand (e.g. `tick`).
    if parts[:2] == ["t3", "loop"] and len(parts) > 2:  # noqa: PLR2004
        return _clean_token(parts[2])[:_LOOP_NAME_MAX] or "loop"

    # 2. Slash-command form: a leading `/foo` or an embedded `/foo` token.
    #    `/loop 5m /babysit-prs` wraps the real command — use the last token.
    slash_tokens = [p for p in parts if p.startswith("/") and len(p) > 1]
    if slash_tokens:
        return _clean_token(slash_tokens[-1].split("/")[-1])[:_LOOP_NAME_MAX] or "loop"

    # 3. Prose: first meaningful word, punctuation/backticks stripped.
    for part in parts:
        cleaned = _clean_token(part)
        if cleaned:
            return cleaned[:_LOOP_NAME_MAX]
    return "loop"


def _load_crons(path: Path) -> dict:
    if not path.is_file():
        return {"jobs": {}, "wakeup": None}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"jobs": {}, "wakeup": None}


def _save_crons(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data) + "\n", encoding="utf-8")


def handle_track_cron_jobs(data: dict) -> None:
    """Track CronCreate/CronDelete/ScheduleWakeup for statusline display."""
    tool_name = data.get("tool_name", "")
    if tool_name not in {"CronCreate", "CronDelete", "ScheduleWakeup"}:
        return

    session_id = data.get("session_id", "")
    if not session_id:
        return

    _ensure_state_dir()
    crons_file = _state_file(session_id, "crons")
    state = _load_crons(crons_file)
    if "jobs" not in state:
        state["jobs"] = {}

    import time  # noqa: PLC0415

    now = int(time.time())
    tool_input = data.get("tool_input", {})

    if tool_name == "CronCreate":
        prompt = tool_input.get("prompt", "")
        cron_expr = tool_input.get("cron", "")
        name = _derive_loop_name(prompt)
        job_id = data.get("tool_result", {}).get("id", "") or f"job-{now}"
        cadence = _cron_cadence_seconds(cron_expr)
        state["jobs"][job_id] = {
            "name": name,
            "cron": cron_expr,
            "cadence": cadence,
            "created_at": now,
        }
        _state_file(session_id, "loop-pending").unlink(missing_ok=True)
    elif tool_name == "CronDelete":
        job_id = tool_input.get("id", "")
        state["jobs"].pop(job_id, None)
    elif tool_name == "ScheduleWakeup":
        delay = int(tool_input.get("delaySeconds", 0))
        reason = tool_input.get("reason", "")
        state["wakeup"] = {
            "name": reason[:30] if reason else "loop",
            "next_epoch": now + delay,
        }

    _save_crons(crons_file, state)


def _cron_cadence_seconds(cron_expr: str) -> int | None:
    """Extract cadence in seconds from simple */N minute patterns."""
    parts = cron_expr.strip().split()
    if len(parts) != 5:  # noqa: PLR2004
        return None
    minute = parts[0]
    if minute.startswith("*/") and all(p == "*" for p in parts[1:]):
        try:
            return int(minute[2:]) * 60
        except ValueError:
            return None
    return None


# ── PreToolUse: block-direct-commands ────────────────────────────────


_REMOTE_DUMP_ENV_RE = re.compile(r"\bT3_ALLOW_REMOTE_DUMP\s*=\s*1\b")
_REMOTE_DUMP_DENY_REASON = (
    "BLOCKED: `T3_ALLOW_REMOTE_DUMP=1` is a removed, defunct bypass (#777) — "
    "setting it does nothing and signals an attempt to circumvent the safety gate. "
    "A fresh remote dump is available only via `t3 <overlay> db refresh --fresh-dump`, "
    "which requires an explicit interactive per-invocation human approval the agent "
    "cannot satisfy. Ask the user to run that command themselves."
)


_SHELL_CHAIN_RE = re.compile(r"[;|`]|\$\(|&&|\|\|")
# Strip both single- and double-quoted literals for the tool-invocation scan so
# that a blocked tool name mentioned inside any quoted argument (e.g. a git
# commit message or a grep pattern) does not false-block the command.
# Value/config patterns (F3, F8) are scanned against the raw command instead,
# so stripping both quote styles here is safe.
_QUOTED_LITERAL_RE = re.compile(r"'[^']*'|\"[^\"]*\"")


def _has_shell_chain(command: str) -> bool:
    """True if *command* contains a shell-chaining operator after the first token.

    Used by F6 fix: a command like ``grep '' /dev/null; blocked-cmd`` starts
    with a read-only prefix but chains a blocked command. The allowlist must
    not short-circuit when a chain operator is present.
    """
    return bool(_SHELL_CHAIN_RE.search(command))


def _deny_match(command: str) -> str | None:
    """Return a deny reason for *command*, or None if it should pass through."""
    # Checked FIRST — even before t3/read-only bypass — because agents must
    # never opt in to remote pg_dump regardless of the surrounding command.
    if _REMOTE_DUMP_ENV_RE.search(command):
        return _REMOTE_DUMP_DENY_REASON
    stripped = command.lstrip()
    # F6: only honor the readonly/t3 prefix allowlist when there is no shell
    # chaining operator in the command. ``grep x /dev/null; pip install y``
    # starts with a read-only prefix but chains a blocked write — the gate
    # must inspect the full command rather than short-circuiting on the prefix.
    if not _has_shell_chain(command) and (_T3_CMD_PREFIX_RE.match(stripped) or _READONLY_CMD_PREFIX_RE.match(stripped)):
        return None
    # Scan VALUE/CONFIG patterns against the raw command so that quoting the
    # value (e.g. ``git -c "core.hooksPath=/dev/null"``) cannot evade the gate.
    for pattern, reason in _RAW_SCAN_BLOCKED:
        if pattern.search(command):
            return reason + " If `t3` fails, fix the CLI — do not work around it."
    # Scan TOOL-INVOCATION patterns against a quote-stripped copy so that a
    # blocked tool name that appears only inside a quoted commit message or grep
    # argument (e.g. ``git commit -m 'fix: handle pip install edge case'``) does
    # not false-block the command.  Real blocked invocations are unquoted and
    # still match the stripped target.
    quote_stripped = _QUOTED_LITERAL_RE.sub(" ", command)
    for pattern, reason in _QUOTE_STRIPPED_BLOCKED:
        if pattern.search(quote_stripped):
            return reason + " If `t3` fails, fix the CLI — do not work around it."
    return None


def handle_block_direct_commands(data: dict) -> bool:
    """Block Bash commands that bypass the t3 CLI.

    Returns True when a deny was emitted (caller should stop the handler chain).
    """
    if data.get("tool_name") != "Bash":
        return False
    command = data.get("tool_input", {}).get("command", "")
    if not command:
        return False
    reason = _deny_match(command)
    if reason is None:
        return False
    return emit_pretooluse_deny(reason)


def handle_block_raw_pid_kill(data: dict) -> bool:
    """Deny a Bash command that signals a process by a raw, guessed pid (#2225).

    The agent has twice killed the WRONG, LIVE process by guessing which
    ``claude`` pid 'looked dead'. A bare ``kill <pid>`` / ``kill -9 <pid>`` at a
    command position is exactly that guessed-pid shape; it is denied so the agent
    must go through the runnable ``t3 teatree safe-kill <pid> --hang-cause``
    command (positive session/task id + non-live proof) instead. ``kill -0``
    (the no-op liveness probe), ``pkill``/``killall`` (signal by name),
    ``%job``/``$VAR``/``$(…)`` targets, and a ``kill`` token inside a comment /
    string / as another command's argument are NOT flagged.

    Because the gate sits on the broad ``Bash`` matcher, its deny is routed
    through :func:`_fail_open_or_deny` so the always-allowed self-rescue commands
    and the master ``[teatree] danger_gate_fail_open`` kill-switch keep it from
    ever wedging a session (the never-lockout contract, #2349). Fails OPEN on any
    import/internal error — a gate bug must never wedge the agent. The handler
    bootstraps ``sys.path`` to import ``teatree`` from the sibling ``src/`` (#1314).
    """
    if data.get("tool_name") != "Bash":
        return False
    command = data.get("tool_input", {}).get("command", "")
    if not command:
        return False
    try:
        with _teatree_src_on_path():
            from teatree.hooks import safe_kill_detect  # noqa: PLC0415

            detection = safe_kill_detect.detect_raw_pid_kill(command)
    except Exception:  # noqa: BLE001
        return False
    if not detection.is_raw_pid_kill:
        return False
    return _fail_open_or_deny(data, detection.message)


# ── PreToolUse: block-secret-file-print (#2306) ──────────────────────
#
# Blocks Bash commands that route a known secret-bearing source to stdout.
# The privacy scan gates COMMITS, but nothing gates a command that echoes a
# secret to the transcript — once it lands there, rotation is the only remedy.
#
# Triggered by:
#   - cat/head/tail of known secret-bearing paths (credential files, key files,
#     .env files, pass stores)
#   - `pass show …` whose stdout is NOT captured or redirected to a file
#   - echo/printf of a pasted token literal (glpat-/ghp_/gho_/xoxb-/xoxp-/sk-)
#
# Allowed (must NOT false-positive):
#   - Reading a value into a shell variable  (VAR=$(…))
#   - Piping / redirecting to a file         (… > out.txt)
#   - Using the value via env or header      (curl -H "Token: $VAR")
#   - cat of ordinary non-secret files
#   - echo of prose that MENTIONS a secret-file path
#
# Fails OPEN on any internal error — a gate bug must never wedge the agent
# (consistent with the #1164 raw-review-post guard).

_SECRET_PATHS_RE = re.compile(  # [skill-load-ok: souliane/teatree repo]
    r"""(?x)
    (?:~|/root|/home/[^/\s]+|/Users/[^/\s]+|\$HOME|\$\{HOME\}|\$\{?HOME\}?)
    /(?:
        \.teatree\.toml
        | \.netrc
        | \.config/gh/hosts\.yml
        | (?:Library/Application\s+Support|\.config)/glab-cli/config\.yml
        | \.ssh/(?:id_[a-z0-9_]+|.*\.pem|.*\.key)
    )
    | (?:^|[\s/])(?:
        \.env(?!\.(?:example|sample|template|dist)\b)(?:\.[a-z]+)?
        | secrets?\.env
        | .*\.credentials?
        | .*\.pem
        | .*\.key
        | .*_account\.json
    )(?:\s|$|['")])
    """,
    re.IGNORECASE,
)

_TOKEN_LITERAL_RE = re.compile(
    r"""(?:^|\s)(?:glpat[-_]|ghp_|gho_|xoxb-|xoxp-|sk-)\S+""",
)

_PRINT_CMDS_RE = re.compile(r"^\s*(?:cat|head|tail)\b")

_PASS_SHOW_RE = re.compile(r"^\s*pass\s+show\b")

_CAPTURE_RE = re.compile(  # [skill-load-ok: souliane/teatree repo]
    r"""
    \$\(            # subshell capture: $(…)
    | >\s*\S+       # stdout redirect to a file or /dev/null
    """,
    re.VERBOSE,
)

_RE_EMITTER_SINK_RE = re.compile(r"^\s*(?:cat|less|more|tee|grep|head|tail)\b")

_ECHO_SAFE_QUOTE_RE = re.compile(r"""^(?:'[^']*'|"[^"]*")$""")

# [skill-load-ok: souliane/teatree repo]
_CREDENTIAL_PRINT_BLOCK_MSG = (
    "BLOCKED: this command would print a secret-bearing file or credential token "
    "to the transcript. Reading a secret into the transcript is irrecoverable — "
    "rotation is the only remedy. Instead, extract the value into a shell variable "
    "(`TOKEN=$(pass show …)`) and use it via env/header without printing it. "
    "Do NOT implement 'mask-then-print' — a masking regex is one edge case away "
    "from leaking. The gate's job is to keep the value off stdout entirely."
)


def _command_captures_or_redirects(command: str) -> bool:
    """Return True when the command's stdout is captured or redirected, not printed.

    A variable-assignment prefix (``VAR=$(…)`` or ``export VAR=$(…)``) or a
    stdout redirect (``> file``) keeps the secret off the transcript. A pipe
    is a capture only when its sink consumes the value — a sink that re-emits
    to the transcript (``cat`` / ``less`` / ``more`` / ``tee`` / ``grep`` /
    ``head`` / ``tail``, incl. ``tee /dev/tty``) still displays the secret and
    is NOT a capture. A plain ``pass show x`` with no such construct prints.
    """
    if _CAPTURE_RE.search(command):
        return True
    segments = command.split("|")
    if len(segments) < 2:  # noqa: PLR2004
        return False
    return not any(_RE_EMITTER_SINK_RE.match(segment) for segment in segments[1:])


def _echo_arg_is_token(command: str) -> bool:
    """Return True when the echo/printf command carries a token literal.

    Prose strings inside quotes that merely MENTION a secret path are not
    treated as token prints — they contain no token literal. A fully-quoted
    arg whose CONTENT is itself a token literal still lands on the transcript,
    so it is treated as a token. Only quoted prose (no token shape) passes.
    """
    parts = command.split(None, 1)
    if len(parts) < 2:  # noqa: PLR2004
        return False
    arg = parts[1].strip()
    if _ECHO_SAFE_QUOTE_RE.match(arg):
        return bool(_TOKEN_LITERAL_RE.search(arg[1:-1]))
    return bool(_TOKEN_LITERAL_RE.search(command))


def _is_secret_print(command: str) -> bool:  # [skill-load-ok: souliane/teatree repo]
    """Whether *command* would print a secret-bearing value to stdout."""
    try:
        if _command_captures_or_redirects(command):
            return False
        if _PRINT_CMDS_RE.match(command):
            return bool(_SECRET_PATHS_RE.search(command))
        if re.match(r"^\s*(?:echo|printf)\b", command):
            return _echo_arg_is_token(command)
        return bool(_PASS_SHOW_RE.match(command))
    except Exception:  # noqa: BLE001
        return False


def handle_block_secret_file_print(data: dict) -> bool:
    """Deny a Bash command that would print a secret-bearing file or token to stdout.

    Blocks cat/head/tail of credential files, ``pass show`` without redirection,
    and echo/printf of pasted token literals. Commands that capture the value
    into a variable or redirect to a file pass through. Returns True when a
    deny was emitted (caller stops the handler chain).
    """
    if data.get("tool_name") != "Bash":
        return False
    command = data.get("tool_input", {}).get("command", "")
    if not command or not _is_secret_print(command):
        return False
    return _fail_open_or_deny(data, _CREDENTIAL_PRINT_BLOCK_MSG)


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
# REST-API merge endpoint: ``(merge_requests|pulls)/<n>/merge``.
# Matches both GitHub (``repos/OWNER/REPO/pulls/<n>/merge``) and
# GitLab (``projects/<id>/merge_requests/<n>/merge``) URL shapes.
_MERGE_ENDPOINT_RE = re.compile(r"(?:merge_requests|pulls)/\d+/merge\b")
_OUT_OF_BAND_MERGE_REASON = (
    "BLOCKED: raw `gh pr merge` / `glab mr merge` on a teatree-managed repo — "
    "an out-of-band merge bypasses the FSM coherence mechanism (ledger update, "
    "MergeClear validation, SHA-binding, privacy/AI-signature scan, mark_merged). "
    "Use the sanctioned keystone transition `t3 <overlay> ticket merge <clear_id>` "
    "(BLUEPRINT §17.1 invariant 8 / §17.4). If this repo is genuinely not "
    "teatree-managed and the cwd could not be resolved, run the merge from inside "
    "the repo's working tree so the gate can classify it."
)


def _is_raw_merge_api_write(command: str) -> bool:
    """Whether *command* is a raw forge REST WRITE to a merge endpoint.

    True only when the command targets a ``.../pulls/<n>/merge`` or
    ``.../merge_requests/<n>/merge`` endpoint AND its EFFECTIVE HTTP method is
    not GET. Reuses the gate-3 effective-method classifier: the LAST
    ``-X``/``--method`` value wins; with no method flag the default is POST
    when a body/field flag is present, else GET. A GET to the merge endpoint
    reads merge status and must NOT be denied.

    Uses a word-boundary regex (not plain ``in``) so double-space variants
    are caught (same class as F4).
    """
    if not _GLAB_GH_API_RE.search(command):
        return False
    if not _MERGE_ENDPOINT_RE.search(command):
        return False
    return _effective_method_is_write(command)


def _overlay_managed_repo_signals() -> tuple[list[str], list[Path]]:
    """Return ``(repo_slug_substrings, overlay_base_paths)`` from config.

    Offline read of ``~/.teatree.toml`` (mirroring :func:`_load_protected_branches`'s
    shape) collecting the two signals that mark a repo teatree-managed: the
    per-overlay repo slug lists (``workspace_repos`` / ``frontend_repos`` /
    ``public_repos``) and each overlay's ``path`` working-tree base. Teatree
    core's own slug (``souliane/teatree``) is always included. Fails to an
    empty signal set on a missing/broken config — the caller treats "no
    resolvable signal + a resolvable slug" as unmanaged, never as a license
    to weaken the gate on uncertainty.
    """
    import tomllib  # noqa: PLC0415

    slugs: list[str] = ["souliane/teatree"]
    paths: list[Path] = []
    config_path = Path.home() / ".teatree.toml"
    if not config_path.is_file():
        return slugs, paths
    try:
        with config_path.open("rb") as f:
            config = tomllib.load(f)
    except Exception:  # noqa: BLE001
        return slugs, paths
    for overlay_cfg in (config.get("overlays") or {}).values():
        if not isinstance(overlay_cfg, dict):
            continue
        for key in ("workspace_repos", "frontend_repos", "public_repos"):
            slugs.extend(str(s).strip().lower() for s in overlay_cfg.get(key, []) if str(s).strip())
        base = overlay_cfg.get("path")
        if isinstance(base, str) and base.strip():
            with contextlib.suppress(OSError, RuntimeError):
                paths.append(Path(base).expanduser().resolve())
    return slugs, paths


@contextlib.contextmanager
def _teatree_src_on_path() -> "Iterator[None]":
    """Put the sibling ``src/`` on ``sys.path`` for the block, then restore it.

    The hook runs in the user's session shell with no guarantee ``teatree`` is
    importable (#1314); this is the shared bootstrap the lazy ``teatree.hooks``
    imports in the merge gate rely on.
    """
    src_dir = str(Path(__file__).resolve().parents[2] / "src")
    added = src_dir not in sys.path
    if added:
        sys.path.insert(0, src_dir)
    try:
        yield
    finally:
        if added:
            with contextlib.suppress(ValueError):
                sys.path.remove(src_dir)


def _cwd_is_teatree_managed(cwd: Path) -> bool | None:
    """Whether *cwd* belongs to a teatree-managed repo.

    Returns ``True`` (managed — keep the keystone-merge block), ``False``
    (unmanaged — allow a raw merge), or ``None`` (cannot classify — the
    caller fails safe and BLOCKS). Reuses ``publish_surface.slug_for_cwd``
    for slug resolution so the host/owner/repo shape matches the
    private-repo carve-out's.
    """
    slugs, paths = _overlay_managed_repo_signals()
    for base in paths:
        with contextlib.suppress(OSError, RuntimeError):
            cwd.resolve().relative_to(base)
            return True
    try:
        with _teatree_src_on_path():
            from teatree.hooks import publish_surface  # noqa: PLC0415

            slug = publish_surface.slug_for_cwd(cwd).lower()
    except Exception:  # noqa: BLE001
        return None
    if not slug:
        return None
    return any(entry in slug for entry in slugs)


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
            from teatree.hooks import raw_merge_detect  # noqa: PLC0415

            return raw_merge_detect.invokes_raw_merge_subcommand(command)
    except Exception:  # noqa: BLE001
        return True


def handle_block_out_of_band_merge(data: dict) -> bool:
    """Block a raw merge command or REST-API merge write on a managed repo.

    Covers two bypass vectors. The literal subcommand form (``gh pr merge`` /
    ``glab mr merge``) is matched action-aware by
    :func:`_invokes_raw_merge_subcommand` — only an actual invocation, not a
    heredoc/echo/comment that documents the phrase (#2387). The REST-API form
    (``gh api .../pulls/<n>/merge -X PUT``, ``glab api
    .../merge_requests/<n>/merge --method POST``) is matched by
    :func:`_is_raw_merge_api_write` (last ``-X``/``--method`` wins; default POST
    with a body flag, else GET). A GET to the merge endpoint reads merge status
    and is NOT denied.

    Carve-out for the permanent-lockout case (#126): a merge is allowed only
    when the cwd repo is confidently NOT teatree-managed. Managed repos and
    any case the gate cannot classify stay BLOCKED — fail-safe on uncertainty.
    """
    if data.get("tool_name") != "Bash":
        return False
    command = data.get("tool_input", {}).get("command", "")
    if not command:
        return False
    if not _invokes_raw_merge_subcommand(command) and not _is_raw_merge_api_write(command):
        return False
    cwd = _resolve_cwd_repo(data)
    if cwd is None:
        return emit_pretooluse_deny(_OUT_OF_BAND_MERGE_REASON)
    managed = _cwd_is_teatree_managed(cwd)
    if managed is False:
        return False
    return emit_pretooluse_deny(_OUT_OF_BAND_MERGE_REASON)


# ── PreToolUse: block-raw-review-post (#1164) ────────────────────────
#
# Sub-agents have repeatedly posted MR/PR review comments by shelling out
# to a raw forge REST POST — ``glab api projects/.../merge_requests/<n>/
# discussions -X POST`` (or ``.../notes``, or the GitHub ``.../pulls/<n>/
# comments``) — bypassing the sanctioned ``t3 <overlay> review post-comment``
# / ``post-draft-note`` path that enforces draft-default (#1207), dedup, and
# on-behalf approval (#960). RED-CARD, 5x recurrence. This gate closes the
# bypass at the Bash boundary: a WRITE to a review discussion/notes/comments
# endpoint is denied; plain GET reads pass through.
#
# Conservative by construction: it matches ONLY the review-comment endpoints
# (discussions / notes / comments) and classifies the command by its EFFECTIVE
# HTTP method — the one gh (2.87.3) / glab (1.80.4) actually send. Both CLIs
# resolve repeated ``-X``/``--method`` flags LAST-WINS (empirically verified:
# ``-X GET -X POST`` POSTs, ``--method GET --method PATCH`` PATCHes), and when
# NO method flag is given they default to POST if a request-body/field flag is
# present, else GET. A command is a READ iff its effective method is GET —
# only then does the forge send ``-f`` as a query parameter rather than a body
# write, so a comment cannot be created (#1568). Every other effective method
# (POST/PUT/PATCH/DELETE/…) is a write. A bare read (``glab api
# .../discussions``) and any non-review endpoint pass through untouched. Fails
# OPEN on an internal parse error — a gate bug must never wedge the fleet.

_REVIEW_POST_ENDPOINT_RE = re.compile(
    r"(?:merge_requests|pulls|issues)/\d+/(?:discussions|notes|comments)\b",
)
# Two captured forms of the gh/glab HTTP-method flag, both empirically valid
# against gh (2.87.3) / glab (1.80.4): the spaced/``=`` form (``-X PUT``,
# ``--method=POST``) and the pflag NO-SPACE shorthand (``-XPUT``). The
# no-space form is a real method override (``gh api -XGET /rate_limit`` returns
# 200), so omitting it let ``-XPUT`` evade classification → ``is_read=True`` →
# the merge/review write slipped through. Consumers flatten the two capture
# groups and keep last-wins effective-method semantics.
_REVIEW_POST_METHOD_RE = re.compile(
    r"(?:-X|--method)[\s=]+['\"]?([A-Za-z]+)\b"
    r"|(?<=-X)([A-Za-z]+)\b",
)
_REVIEW_POST_BODY_FLAG_RE = re.compile(
    r"(?:^|\s)(?:-f|--field|-F|--raw-field|--input|-d|--data)\b",
)
_REVIEW_POST_DENY_REASON = (
    "BLOCKED: raw `glab api`/`gh api` POST to a review discussion/notes/comments "
    "endpoint bypasses the sanctioned review-post CLI. To CREATE a note use "
    "`t3 <overlay> review post-comment` (draft by default, #1207) or `post-draft-note`; "
    "to EDIT use `t3 <overlay> review update-note`; to REMOVE use `delete-discussion` (MR) "
    "or `delete-issue-note` (issue/work-item) — the CLI enforces draft-default, dedup, and "
    "on-behalf approval, which a direct REST write skips. Read-only GETs are unaffected."
)


_GLAB_GH_API_RE = re.compile(r"\b(?:glab|gh)\s+api\b")


def _is_raw_review_write(command: str) -> bool:
    """Whether *command* is a raw forge REST WRITE to a review-comment endpoint.

    True only when the command targets a ``.../discussions``, ``.../notes``,
    or ``.../comments`` endpoint AND its EFFECTIVE HTTP method is not GET. The
    effective method models gh/glab semantics: the LAST ``-X``/``--method``
    value wins (so ``-X GET -X POST`` is a POST write, ``-X POST -X GET`` is a
    GET read); with no method flag the forge defaults to POST when a body/field
    flag is present, else GET. A forced GET sends body flags as query params
    and cannot create a comment, so it is the only read (#1568).

    Uses a word-boundary regex (not plain ``in``) so ``glab  api`` /
    ``gh  api`` double-space variants are caught (F4).
    """
    if not _GLAB_GH_API_RE.search(command):
        return False
    if not _REVIEW_POST_ENDPOINT_RE.search(command):
        return False
    methods = [m.upper() for pair in _REVIEW_POST_METHOD_RE.findall(command) for m in pair if m]
    if methods:
        is_read = methods[-1] == "GET"
    elif _REVIEW_POST_BODY_FLAG_RE.search(command):
        is_read = False
    else:
        is_read = True
    return not is_read


def handle_block_raw_review_post(data: dict) -> bool:
    """Deny a raw ``glab api``/``gh api`` WRITE to a review-comment endpoint.

    Forces the sanctioned ``t3 <overlay> review post-comment`` /
    ``post-draft-note`` path (draft-default + dedup + on-behalf approval),
    which a direct REST write skips. Conservative: a command is denied only
    when its effective HTTP method (last ``-X``/``--method`` wins; default POST
    when a body flag is present) is not GET. Reads — bare, explicit-GET, or
    write-then-GET — and non-review endpoints pass through. Returns True when a
    deny was emitted (caller stops the handler chain).
    """
    if data.get("tool_name") != "Bash":
        return False
    command = data.get("tool_input", {}).get("command", "")
    if not command or not _is_raw_review_write(command):
        return False
    return emit_pretooluse_deny(_REVIEW_POST_DENY_REASON)


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

_SLACK_POST_TIMEOUT_SECONDS = 2.0


def _slack_http_poster():  # noqa: ANN202 — Poster protocol from the lazily-imported leaf.
    """Build the hook-budget Slack poster: ``SlackHttpClient.post``, no retry.

    The mirror runs synchronously inside the ~5s hook timeout, so the client
    carries the short per-call timeout and NO retry (a retry-with-backoff could
    blow the budget). This is the router's platform→domain edge (the router is
    tach-invisible), injected into the pure leaf.
    """
    from teatree.backends.slack.http import SlackHttpClient  # noqa: PLC0415

    return SlackHttpClient(timeout=_SLACK_POST_TIMEOUT_SECONDS, max_retries=0).post


def _active_dm_thread_for_channel(channel: str) -> str:
    """Resolve the user's active DM thread for ``channel`` from ``IncomingEvent``.

    Threads the mirrored question under the conversation the user is already in
    instead of opening a new top-level message. Fail-open: any bootstrap or DB
    error yields ``""`` (post at root) so the hook stays crash-proof.
    """
    if not channel or not bootstrap_teatree_django():
        return ""
    try:
        from teatree.core.models import IncomingEvent  # noqa: PLC0415

        return IncomingEvent.objects.active_dm_thread(channel=channel)
    except Exception:  # noqa: BLE001
        return ""


def _slack_config_from_toml() -> tuple[str, str] | None:
    from teatree.hooks.slack_mirror import slack_config_from_toml  # noqa: PLC0415

    return slack_config_from_toml()


def _perform_slack_post(slack_cfg: tuple[str, str], questions: list[dict]) -> str:
    from teatree.hooks.slack_mirror import perform_slack_post  # noqa: PLC0415

    return perform_slack_post(
        slack_cfg,
        questions,
        poster=_slack_http_poster(),
        resolve_thread=_active_dm_thread_for_channel,
    )


def _read_dm_channel_cache(user_id: str) -> str:
    from teatree.hooks.slack_mirror import read_dm_channel_cache  # noqa: PLC0415

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
    """Mirror a present-mode ``AskUserQuestion`` to Slack; deny a loop-driven one (#1174).

    Runs LAST in the PreToolUse chain (the away handler ran first and
    already short-circuited an away turn). Three present-mode arms:

    - live user turn (the user typed a prompt seconds ago, in this
    session) — mirror to Slack and return ``False`` so the question
    renders in-client. Preserves ``TestPresentModeMirrorsButDoesNotDeny``
    and the #189 live-turn escape.
    - attended non-owner turn (a different live session owns the loop; a
    human is reading the prose) — mirror and return ``False``.
    - loop-driven / autonomous turn (this session drives the loop, or
    there is no live owner) — the broken path: rendering in-client
    suspends the session with no way for a Slack reply to reach it.
    Instead capture a generation-stamped mirror-linked
    ``DeferredQuestion``, then deny so the agent narrates the deferral and
    proceeds; the answer arrives later via ``additionalContext``.
    """
    if data.get("tool_name") != "AskUserQuestion":
        return False
    if _is_live_user_turn(data) or not _session_drives_loop(str(data.get("session_id", ""))):
        _post_question_to_slack(data)
        return False
    if not str(_first_question(data).get("question", "")).strip():
        _post_question_to_slack(data)
        return False
    queue_id = _capture_and_defer_question(data, mode="present")
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


_AWAY_MIRROR_SUFFIX = "away-question-mirror"


def _away_mirror_key(question: dict) -> str:
    """Stable hash of the recorded question — the idempotency key.

    The marker file is already namespaced by ``session_id`` (it is the
    ``_state_file`` name), so the hash need not repeat the session.
    """
    blob = json.dumps(question, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _mirror_question_to_slack(question: dict, session_id: str, *, mode: str) -> tuple[str, str]:
    """Post the single recorded question to the user's Slack DM; return ``(ts, channel)``.

    Both the away-mode handler and the present-mode deny arm mirror through
    here (the user reads Slack, not ``t3 teatree questions list``). Mirrors only
    the single recorded question, not the full payload, so the DM never
    shows more rows than are answerable. In ``away`` mode an idempotent
    session-namespaced STATE_DIR marker keyed on a stable hash of the
    question stops a harness retry of the same tool call double-posting;
    present mode relies on generation supersession instead. The returned
    ``ts``/``channel`` link the mirror DM to its :class:`DeferredQuestion`
    so a later Slack reply can bind the live generation. Fail-open: any
    Slack/IO error yields ``("", "")`` so the deny is never blocked and
    the loop never wedges.
    """
    if not question:
        return "", ""
    slack_cfg = _slack_config_from_toml()
    if slack_cfg is None:
        return "", ""
    try:
        if mode == "away":
            key = _away_mirror_key(question)
            marker = _state_file(session_id or "no-session", _AWAY_MIRROR_SUFFIX)
            if key in _read_lines(marker):
                return "", ""
            ts = _perform_slack_post(slack_cfg, [question])
            with contextlib.suppress(OSError):
                _ensure_state_dir()
                _append_line(marker, key)
        else:
            ts = _perform_slack_post(slack_cfg, [question])
    except Exception:  # noqa: BLE001 — a Slack failure never blocks the capture/deny.
        return "", ""
    return ts, _read_dm_channel_cache(slack_cfg[1])


# ── PreToolUse: route-away-mode-question (#58, BLUEPRINT §17.1 invariant 9) ────


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
    """SHA-256 of canonicalized options — same shape as :func:`_away_mirror_key`."""
    blob = json.dumps(options, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _first_question(data: dict) -> dict:
    questions = data.get("tool_input", {}).get("questions", []) or []
    first = questions[0] if isinstance(questions, list) and questions else {}
    return first if isinstance(first, dict) else {}


def _capture_and_defer_question(data: dict, *, mode: str) -> int | None:
    """Record one mirror-linked ``DeferredQuestion`` and post it to Slack.

    The single chokepoint both the away-mode handler and the present-mode
    deny arm call (#1174). It supersedes any pending older-generation row
    for the same (session, run), posts the question to the user's Slack DM
    capturing the posted ``ts``, and records the row with its mirror
    fields so the reply matcher can bind a later Slack reply to exactly
    this generation. Returns the new row id, or ``None`` when teatree is
    unavailable (the caller then fails open — the in-client modal renders).
    """
    if not bootstrap_teatree_django():
        return None
    try:
        from teatree.core.models.deferred_question import DeferredQuestion  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return None
    first = _first_question(data)
    question_text = str(first.get("question", "")).strip()
    if not question_text:
        return None
    options = first.get("options", []) if isinstance(first.get("options"), list) else []
    session_id = str(data.get("session_id", ""))
    run_id = _run_id(data)
    try:
        generation = DeferredQuestion.next_generation(session_id=session_id, run_id=run_id)
        for prior in DeferredQuestion.objects.filter(
            session_id=session_id, run_id=run_id, answered_at__isnull=True, dismissed_at__isnull=True
        ):
            prior.mark_stale("superseded by newer question")
    except Exception:  # noqa: BLE001
        return None
    slack_ts, slack_channel = _mirror_question_to_slack(first, session_id, mode=mode)
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
        )
    except Exception:  # noqa: BLE001
        return None
    return int(row.pk)


def _resolved_away_mode() -> bool:
    """Resolve the effective availability mode; True when ``away`` (#2559).

    Delegates to the stdlib sibling :func:`availability_away_probe.resolved_away_mode`,
    which reads the resolved mode by subprocessing ``t3 <overlay> availability
    show`` instead of an in-process ``django.setup()`` — the bare-``python3``
    hook has no ``uv`` env, so the old bootstrap returned ``False`` (never away)
    and silently neutered ``t3 <overlay> availability away`` as a suppressor. The
    thin wrapper stays here as the single patchable seam every caller and test
    already targets.
    """
    return resolved_away_mode_stdlib()


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
        from teatree.core import availability  # noqa: PLC0415

        return availability.PRESENCE.is_live_user_turn(session_id=str(data.get("session_id", "")))
    except Exception:  # noqa: BLE001
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
        from teatree.core import availability  # noqa: PLC0415

        availability.PRESENCE.refresh_live_turn(session_id=str(data.get("session_id", "")))
    except Exception:  # noqa: BLE001
        return


def handle_route_away_mode_question(data: dict) -> bool:
    """Convert an ``AskUserQuestion`` to a ``DeferredQuestion`` when availability=away.

    Runs FIRST in the PreToolUse chain for ``AskUserQuestion`` and denies,
    short-circuiting the chain before the present-mode
    ``handle_mirror_question_to_slack`` (the last handler) would run. So
    this handler is the only place that can mirror an away-mode question
    to the user's Slack DM — and it does, between recording the row and
    emitting the deny (the user reads Slack, not ``t3 teatree questions list``).
    Returns ``True`` with a ``permissionDecision=deny`` and a friendly
    reason that names the recorded row so the agent narrates the
    conversion correctly. The denied tool_use block still appears in the
    transcript, so the §807 structured-question Stop gate
    ``_last_assistant_turn`` detects ``used_question_tool=True`` and lets
    the turn complete.

    Exception (#189): on a USER-DRIVEN turn — a fresh same-session
    ``UserPromptSubmit`` within ``LIVE_TURN_FRESHNESS`` — the question
    renders in-client instead of deferring, even under a manual-away
    override. That is what lets ``/checking`` walk the user through the
    backlog without flipping availability. An autonomous / loop-driven
    turn is not live, so it still defers (invariant 9 intact).
    """
    if data.get("tool_name") != "AskUserQuestion":
        return False
    if not _resolved_away_mode():
        return False
    if _is_live_user_turn(data):
        # The user is driving THIS turn (a fresh same-session prompt seconds
        # ago) — let the question render in-client even under away. This is
        # the #189 escape that makes `/checking` work without an availability
        # flip. An autonomous / loop-driven turn is NOT live, so it still
        # defers below — invariant 9 holds for the loop's own questions.
        #
        # Slide the live window forward: the user answering this question is
        # fresh evidence they are still driving, so the NEXT question in the
        # same walk-through stays live even after an intervening background
        # task-notification turn (which never refreshes the heartbeat) — #2058.
        _refresh_live_turn(data)
        return False
    if not str(_first_question(data).get("question", "")).strip():
        # No question text — fail open rather than emit a deny that
        # blocks an empty payload the user can debug separately.
        return False
    queue_id = _capture_and_defer_question(data, mode="away")
    if queue_id is None:
        # Teatree unavailable — fail open so the user is never blocked
        # by a hook crash. The standard interactive flow then runs.
        return False
    reason = (
        f"availability=away — your question was captured durably as DeferredQuestion #{queue_id} "
        f"and the user will answer it via `t3 teatree questions answer {queue_id} <text>`. "
        "Proceed with any work that does not depend on the answer; the response will surface "
        "in a future turn's additionalContext when the user resolves it."
    )
    return emit_pretooluse_deny(reason)


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
    if not bootstrap_teatree_django():
        return
    try:
        from teatree.core.availability import pending_questions_count  # noqa: PLC0415
        from teatree.core.models.deferred_question import DeferredQuestion  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return
    session_id = str(data.get("session_id", ""))
    try:
        answered = list(DeferredQuestion.answered_not_applied(session_id=session_id)[:5])
    except Exception:  # noqa: BLE001
        answered = []
    for row in answered:
        with contextlib.suppress(Exception):
            if DeferredQuestion.mark_applied(row.pk):
                print(  # noqa: T201
                    f"Your AskUserQuestion (#{row.pk}) was answered by the user on Slack: "
                    f'"{row.answer_text}". Apply it now.'
                )
    try:
        count = pending_questions_count()
        if count == 0:
            return
        rows = list(DeferredQuestion.pending()[:5])
    except Exception:  # noqa: BLE001
        return
    lines = [f"You have {count} deferred question(s) awaiting user answer:"]
    lines.extend(f"  #{row.pk} — {row.question[:120]}" for row in rows)
    print("\n".join(lines))  # noqa: T201


# ── UserPromptSubmit: inject pending Slack-DM backlog into context ─────────────
#
# Inbound half of the Slack ↔ Claude-Code bidirectional bridge (#1014,
# BLUEPRINT §17.1 invariant 2 / §5.6). The user only reads Slack DMs;
# their reply to the overlay bot lands here as a ``PendingChatInjection``
# row. The next ``UserPromptSubmit`` drain reads unconsumed rows for the
# loop-owner session and emits them into ``additionalContext`` — the
# agent sees the message as if the user had typed it in chat.


def handle_inject_pending_chat(data: dict) -> None:
    """Append unconsumed Slack-DM messages to the next prompt's ``additionalContext``.

    **Drain eligibility:** ANY interactive Claude Code session that
    receives a ``UserPromptSubmit`` event may drain the queue. The
    original implementation gated on ``_session_owns_loop`` (mirroring
    the §5.6 ``handle_loop_self_pump`` discipline), but the loop-owner
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
    if not bootstrap_teatree_django():
        return
    try:
        from teatree.core.models.pending_chat_injection import PendingChatInjection  # noqa: PLC0415
    except Exception:  # noqa: BLE001 — fail open: queue survives to the next tick
        return
    try:
        rows = list(PendingChatInjection.pending())
    except Exception:  # noqa: BLE001
        return
    drained: list[str] = [f"User replied on Slack at {row.slack_ts}: {row.text}" for row in rows if row.consume()]
    if not drained:
        return
    header = f"You have {len(drained)} new Slack DM reply(ies) from the user:"
    print("\n".join([header, *drained]))  # noqa: T201


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
        from datetime import timedelta  # noqa: PLC0415

        from teatree.core.models.pending_chat_injection import PendingChatInjection  # noqa: PLC0415
    except Exception:  # noqa: BLE001 — fail open: nag re-tries next turn
        return None
    try:
        rows = PendingChatInjection.unanswered_questions_since(timedelta(hours=_ANSWERED_GATE_WINDOW_HOURS))
    except Exception:  # noqa: BLE001
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


_EDIT_TOOL_NAMES = frozenset({"Edit", "Write", "NotebookEdit"})


def _edit_block_path(block: dict) -> str | None:
    """File path for an ``Edit``/``Write``/``NotebookEdit`` tool_use block.

    Caller pre-filters with ``isinstance(block, dict)`` (mirrors the
    ``_block_is_settings_write`` contract).
    """
    if block.get("type") != "tool_use":
        return None
    name = block.get("name")
    if name not in _EDIT_TOOL_NAMES:
        return None
    tool_input = block.get("input")
    if not isinstance(tool_input, dict):
        return None
    raw = tool_input.get("file_path") or tool_input.get("notebook_path")
    if isinstance(raw, str) and raw:
        return raw
    return None


def _current_turn_edits(transcript_path: str) -> list[str]:
    """File paths edited by the assistant in the most recent turn.

    Walks the transcript newest→oldest; the most recent ``user`` entry
    is the boundary. Returns the file paths from every ``Edit`` /
    ``Write`` / ``NotebookEdit`` ``tool_use`` block after that
    boundary, in transcript order. Duplicates kept — the caller
    classifies + dedupes.
    """
    entries = _read_transcript_entries(transcript_path)
    if not entries:
        return []
    edits: list[str] = []
    for entry in reversed(entries):
        role = _entry_role(entry)
        if role == "user":
            break
        if role != "assistant":
            continue
        for block in _entry_content(entry):
            if not isinstance(block, dict):
                continue
            path = _edit_block_path(block)
            if path is not None:
                edits.append(path)
    edits.reverse()
    return edits


def _current_turn_assistant_text(transcript_path: str) -> str:
    """Concatenated assistant text blocks in the most recent turn.

    Used to detect a teatree-issue reference that clears the gate.
    """
    chunks: list[str] = []
    entries = _read_transcript_entries(transcript_path)
    for entry in reversed(entries):
        role = _entry_role(entry)
        if role == "user":
            break
        if role != "assistant":
            continue
        for block in _entry_content(entry):
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    chunks.append(text)
    return "\n".join(chunks)


# ── Stop: speak-on-stop arm (local == all, #2060) ───────────────────────────


def _speak_settings() -> tuple[str, bool]:
    """Read ``[teatree.speak]`` from ``~/.teatree.toml`` → ``(local, slack)`` (#2060).

    The hook-side mirror of :func:`teatree.config_speak.resolve_speak` (the hook
    cannot cheaply import the Django config, so it re-reads the toml with the
    SAME precedence — a parity test pins the two in agreement): an explicit
    ``[teatree.speak]`` sub-table wins, else the defaults (``"off", False``).
    ``local`` is the :class:`~teatree.types.LocalPlayback` value
    (``off``/``dm``/``all``). Best-effort: a missing or malformed config, or
    no ``[teatree]`` table, yields the defaults so the Stop arm stays silent
    unless the user opted in.
    """
    import tomllib  # noqa: PLC0415

    config_path = Path.home() / ".teatree.toml"
    if not config_path.is_file():
        return "off", False
    try:
        with config_path.open("rb") as f:
            config = tomllib.load(f)
    except Exception:  # noqa: BLE001 — Stop hook must be crash-proof
        return "off", False
    teatree = config.get("teatree") if isinstance(config, dict) else None
    if not isinstance(teatree, dict):
        return "off", False
    subtable = teatree.get("speak")
    if isinstance(subtable, dict):
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
    from teatree.hooks import closure_reverify_scanner  # noqa: PLC0415

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
    assistant_text = _current_turn_assistant_text(transcript_path)
    if _TEATREE_ISSUE_REF.search(assistant_text):
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


# ── Classifier-denial STOP gate (#1247) ─────────────────────────────
#
# When the auto-mode classifier denies a tool call the agent must STOP
# and explain (action / reason / minimum-unblock) per the binding
# "Classifier Denial Protocol" in skills/rules/SKILL.md.  Prose-only
# enforcement has slipped repeatedly — the gate makes it deterministic:
#
# 1. PostToolUse scans every tool_response for the canonical denial
#    preamble — the exact phrase the harness emits on classifier
#    deny (see ``_CLASSIFIER_DENIAL_PREAMBLE`` below).  On a match it
#    writes a per-session marker carrying the action fingerprint
#    (tool name + short input excerpt).
# 2. Stop reads the marker.  If present it emits a top-level
#    ``systemMessage`` reminding the agent to STOP and explain.
#    Returns True to break the Stop chain so the message survives.
# 3. The next UserPromptSubmit clears the marker — the fresh user
#    turn carries the explicit per-call authorisation (or a redirect),
#    so the gate auto-disarms.
#
# Fail-safe-to-empty: handler returns silently on malformed input or
# missing fields — the hook must NEVER crash the harness.

_CLASSIFIER_DENIAL_PREAMBLE = "denied by the Claude Code auto mode classifier"
_CLASSIFIER_DENY_MARKER_SUFFIX = "classifier-deny"
_CLASSIFIER_DENY_ACTION_EXCERPT_MAX = 120


_DENIAL_RESPONSE_STRING_KEYS = ("error", "content", "stderr", "stdout", "message", "output", "reason")


def _tool_response_strings(tool_response: object) -> list[str]:
    """Return every string value reachable from ``tool_response`` (shallow).

    The classifier denial can land in ``error``, ``content``, ``stderr``,
    ``message``, ``output``, or as a bare string.  We scan a fixed set of
    likely keys rather than recursing — keeps the detector cheap and
    predictable.  Fail-safe-to-empty on unexpected shapes.
    """
    from typing import cast  # noqa: PLC0415

    if isinstance(tool_response, str):
        return [tool_response]
    if not isinstance(tool_response, dict):
        return []
    response = cast("dict[str, object]", tool_response)
    out: list[str] = []
    for key in _DENIAL_RESPONSE_STRING_KEYS:
        value = response.get(key)
        if isinstance(value, str):
            out.append(value)
    return out


def _format_action_excerpt(tool_name: str, tool_input: object) -> str:
    """Build a short ``<tool_name>: <input>`` excerpt naming the denied action.

    Truncates to ``_CLASSIFIER_DENY_ACTION_EXCERPT_MAX`` characters so the
    Stop gate's systemMessage stays one line.  Tries the common
    descriptive keys (``command``, ``file_path``, ``prompt``) before
    falling back to the repr of the full input.
    """
    from typing import cast  # noqa: PLC0415

    name = tool_name if isinstance(tool_name, str) else "tool"
    excerpt = name
    if isinstance(tool_input, dict):
        input_dict = cast("dict[str, object]", tool_input)
        excerpt = f"{name}: {input_dict!r}"
        for key in ("command", "file_path", "prompt", "url", "channel"):
            value = input_dict.get(key)
            if isinstance(value, str) and value:
                excerpt = f"{name}: {value}"
                break
    if len(excerpt) > _CLASSIFIER_DENY_ACTION_EXCERPT_MAX:
        excerpt = excerpt[: _CLASSIFIER_DENY_ACTION_EXCERPT_MAX - 1] + "…"
    return excerpt


def handle_track_classifier_denial(data: dict) -> None:
    """PostToolUse: persist a marker when the classifier denies a tool call.

    Scans the ``tool_response`` payload for the canonical denial preamble
    and writes ``<session_id>.classifier-deny`` carrying enough context
    for the Stop gate to name what was denied.  Returns silently on any
    missing/malformed field — fail-safe-to-empty per the spec.
    """
    if not isinstance(data, dict):
        return
    session_id = data.get("session_id", "")
    if not isinstance(session_id, str) or not session_id:
        return
    tool_response = data.get("tool_response")
    if tool_response is None:
        return
    strings = _tool_response_strings(tool_response)
    if not any(_CLASSIFIER_DENIAL_PREAMBLE in s for s in strings):
        return
    tool_name = data.get("tool_name", "")
    tool_input = data.get("tool_input")
    excerpt = _format_action_excerpt(tool_name, tool_input)
    payload = {
        "tool_name": tool_name if isinstance(tool_name, str) else "",
        "action": excerpt,
    }
    try:
        _ensure_state_dir()
        marker = _state_file(session_id, _CLASSIFIER_DENY_MARKER_SUFFIX)
        marker.write_text(json.dumps(payload), encoding="utf-8")
    except OSError:
        # Fail-safe: a write failure must not crash the harness.
        return


def handle_classifier_deny_stop_gate(data: dict) -> bool | None:
    """Stop: emit STOP-and-explain ``systemMessage`` if a denial is pending.

    Returns ``True`` to break the Stop chain (mirrors the consideration
    gate pattern) when the marker exists.  Otherwise returns ``None``
    so the rest of the Stop chain runs unchanged.
    """
    if not isinstance(data, dict):
        return None
    session_id = data.get("session_id", "")
    if not isinstance(session_id, str) or not session_id:
        return None
    marker = _state_file(session_id, _CLASSIFIER_DENY_MARKER_SUFFIX)
    if not marker.is_file():
        return None
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    action = payload.get("action") or payload.get("tool_name") or "the denied tool call"
    body = (
        f"Classifier denied {action}. STOP and explain: action / reason / "
        'minimum-unblock — per the binding "Classifier Denial Protocol" '
        "(skills/rules/SKILL.md). Do not retry with a different argument "
        "shape, decompose the command, or switch tools. Ask the user via "
        'AskUserQuestion with two options: "Allow it (relax classifier)" '
        'or "Keep the denial (do it differently)".'
    )
    # Stop schema reserves ``hookSpecificOutput.additionalContext`` for
    # other events — emit the top-level ``systemMessage`` (schema-valid;
    # non-decision; visible to the agent) so the nag survives.
    json.dump({"systemMessage": body}, sys.stdout)
    return True


def handle_clear_classifier_deny_marker(data: dict) -> None:
    """UserPromptSubmit: clear the classifier-deny marker for this session.

    The next user turn re-arms the gate — the user either grants the
    per-call authorisation explicitly (which the agent now relays) or
    redirects to a different approach.  Either way the previous denial
    is no longer the active blocker.
    """
    if not isinstance(data, dict):
        return
    session_id = data.get("session_id", "")
    if not isinstance(session_id, str) or not session_id:
        return
    marker = _state_file(session_id, _CLASSIFIER_DENY_MARKER_SUFFIX)
    try:
        marker.unlink(missing_ok=True)
    except OSError:
        return


# ── Router ──────────────────────────────────────────────────────────

# ── SubagentStop: record a sub-agent that terminated without committing ──
#
# Issue #1205: an ``isolation: worktree`` sub-agent that only edits files and
# never commits loses ALL its work when the worktree is auto-cleaned on
# teardown, yet the orchestrator believes work landed — a phantom-completion
# source (3x recurrence). This SubagentStop handler runs once per sub-agent
# termination and, when the sub-agent's worktree shows a WORK branch with ZERO
# commits ahead of its base, records a ``terminated_without_commit`` signal so
# the orchestrator can SEE the empty termination instead of assuming success.
#
# It is a DETECTION/surfacing hook, not a deny — SubagentStop cannot
# un-terminate the agent. The signal is recorded through the SAME durable seam
# the dispatched-sub-agent roster uses: a per-session ``<session>.no-commit``
# state file (mirrors ``<session>.agents``), which the PreCompact recovery
# snapshot already reads back and renders so it survives compaction. A
# structured stderr line (this module's logging channel) carries the same fact
# for the live transcript.
#
# Crash-proof and conservative (the #810 Stop-hook contract): a detached/
# read-only review worktree (detached HEAD or a base branch) and a sub-agent
# that DID commit are NOT flagged, and ANY inability to introspect git fails
# OPEN (never flag) — a detection bug must never manufacture a false alarm.
#
# Limitation: the SubagentStop payload's ``cwd`` is the only reliable handle on
# the sub-agent's worktree (the harness does not carry a dedicated worktree-path
# field, and ``transcript_path`` points at the parent session — see the #115
# note above). For a worktree-isolated sub-agent the harness ``cwd`` IS the
# worktree, so it is the right signal; when ``cwd`` is absent the handler is a
# clean no-op rather than guessing.


def _record_no_commit_signal(session_id: str, finding: object) -> None:
    r"""Persist + log one ``terminated_without_commit`` signal.

    Durable channel: append a deduped ``<branch>\t<worktree>`` line to the
    per-session ``<session>.no-commit`` state file (same shape/seam as the
    ``<session>.agents`` roster, which the PreCompact snapshot reads back).
    Live channel: a structured stderr line. Best-effort — a record failure
    must never propagate out of the Stop hook.
    """
    branch = getattr(finding, "branch", "") or "(unknown)"
    worktree = getattr(finding, "worktree", "") or "(unknown)"
    print(  # noqa: T201 — hook stderr is the module's logging channel
        f"[hook_router] terminated_without_commit — sub-agent left work branch "
        f"{branch!r} at {worktree!r} with 0 commits; work would be lost on worktree teardown.",
        file=sys.stderr,
    )
    if not session_id:
        return
    with contextlib.suppress(OSError):
        _ensure_state_dir()
        no_commit_file = _state_file(session_id, "no-commit")
        line = f"{branch}\t{worktree}"
        if line not in _read_lines(no_commit_file):
            _append_line(no_commit_file, line)


def _capture_subagent_snapshot(worktree: str, branch: str, label: str) -> None:
    """Capture a bundle+diff of a dirty/unpushed sub-agent worktree (#1764).

    Runs under bare ``python3`` from the SubagentStop hook, so it imports only
    the Django-free :mod:`teatree.core.worktree_snapshot`. The snapshot lands
    BEFORE any teardown can auto-clean the worktree, preserving uncommitted
    edits and unpushed commits an outage-killed sub-agent left behind. ``git
    bundle`` runs against the worktree's own object store (the worktree shares
    the main clone's gitdir), so the worktree path doubles as the repo handle.
    Best-effort: a no-op for a clean+pushed tree, fully crash-proof at the
    caller's boundary.
    """
    from teatree.core.worktree_snapshot import capture_worktree_snapshot  # noqa: PLC0415

    recovery_dir = capture_worktree_snapshot(Path(worktree), worktree, branch=branch, label=label)
    if recovery_dir is not None:
        print(  # noqa: T201 — hook stderr is the module's logging channel
            f"[hook_router] sub-agent worktree {worktree!r} (branch {branch!r}) had dirty/unpushed work — "
            f"captured recovery artifact to {recovery_dir} before teardown.",
            file=sys.stderr,
        )


def handle_subagent_stop_no_commit(data: dict) -> None:
    """SubagentStop: record a work-branch worktree that produced 0 commits (#1205).

    Also captures a recovery snapshot (#1764) of the sub-agent's worktree
    (resolved to a work branch) BEFORE teardown can auto-clean it — the
    Django-free snapshot no-ops on a clean+pushed tree and writes a bundle+diff
    when there are uncommitted edits or unpushed commits, so an outage-killed
    sub-agent's work survives.

    Resolves the sub-agent's worktree from the harness ``cwd``, runs the
    conservative :func:`teatree.hooks.no_commit_detector.detect`, and records a
    ``terminated_without_commit`` signal only on the confirmed-flag verdict.
    No-op for a read-only/detached worktree, a committed branch, an
    undeterminable git state, or a missing ``cwd``.

    Crash-proof (#810 Stop contract): a broad boundary guard contains any
    unexpected error (an unimportable ``teatree``, git introspection failure)
    to a single stderr line — the sub-agent terminates normally and the
    detection is simply skipped (fail open).
    """
    try:
        worktree = data.get("cwd", "")
        if not worktree:
            return
        src_dir = Path(__file__).resolve().parents[2] / "src"
        if str(src_dir) not in sys.path:
            sys.path.insert(0, str(src_dir))
        from teatree.hooks import no_commit_detector  # noqa: PLC0415
        from teatree.utils import git  # noqa: PLC0415

        finding = no_commit_detector.detect(worktree)
        if finding.is_flagged:
            _record_no_commit_signal(data.get("session_id", ""), finding)

        branch = git.current_branch(repo=worktree)
        if branch and branch not in no_commit_detector.NON_WORK_BRANCHES:
            _capture_subagent_snapshot(worktree, branch, branch)
    except Exception as exc:  # noqa: BLE001 — SubagentStop hook must be crash-proof
        print(  # noqa: T201 — hook stderr is the module's logging channel
            f"[hook_router] no-commit detection skipped (unexpected error: {exc})",
            file=sys.stderr,
        )


_HANDLERS: dict[str, list] = {
    "UserPromptSubmit": [
        handle_clear_classifier_deny_marker,
        handle_reset_turn_tool_budget,
        handle_record_presence,
        handle_enforce_loop_on_prompt,
        handle_todo_freshness_nudge,
        handle_inject_pending_questions,
        handle_inject_pending_chat,
        handle_user_prompt_submit,
    ],
    "PreToolUse": [
        handle_allow_classifier_relax_settings_write,
        handle_route_away_mode_question,
        handle_enforce_loop_registration,
        handle_block_edit_before_planned,
        handle_block_config_overwrite,
        handle_protect_default_branch,
        handle_block_self_dm_via_mcp,
        handle_quote_scanner_pretool,
        handle_dispatch_prompt_quote_scanner,
        handle_banned_terms_pretool,
        handle_enforce_skill_loading,
        handle_block_direct_commands,
        handle_block_raw_pid_kill,
        handle_block_secret_file_print,
        handle_block_out_of_band_merge,
        handle_block_unknown_repo_push,
        handle_block_raw_review_post,
        handle_validate_mr_metadata,
        handle_block_self_reviewer_assign,
        handle_block_ai_signature,
        handle_block_uncovered_diff,
        handle_enforce_orchestrator_boundary,
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
    ],
    "TaskCreated": [
        handle_enforce_skill_loading_on_task_create,
        handle_dispatch_prompt_quote_scanner_on_task_create,
    ],
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
        handle_completion_claim_gate,
        handle_enforce_answered_questions,
        handle_closure_reverify_stop,
        handle_consideration_gate,
        handle_speak_all_on_stop,
        handle_loop_self_pump,
    ],
    "SubagentStop": [handle_subagent_stop_no_commit],
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
        # deny payload. Only an explicit ``True`` return is a deny — it stops the
        # chain to avoid writing multiple JSON objects to stdout (invalid JSON).
        try:
            verdict = handler(data)
        except Exception:  # noqa: BLE001 — crash-proof router: a broken gate fails open, never denies.
            traceback.print_exc(file=sys.stderr)
            continue
        if verdict is True:
            deny_emitted = True
            break

    # A PreToolUse call that ran the whole chain without a deny is genuine
    # progress: reset the deny-streak so only CONSECUTIVE identical denials
    # accumulate in the circuit breaker.
    if args.event == "PreToolUse" and not deny_emitted:
        _reset_deny_streak(data.get("session_id", ""))

    # Exit-code contract is per-event. PreToolUse / TaskCreated denies are only
    # honoured at exit code 2 (#1447) and their reason rides ``hookSpecificOutput``
    # / ``continue:false`` on stdout, which the harness reads even at exit 2.
    # Stop / SubagentStop and the other top-level-``decision`` events INVERT this:
    # exit 2 is a blocking error that makes the harness discard the stdout JSON
    # and read stderr instead — so a Stop block must exit 0 to let its
    # ``{"decision":"block","reason":...}`` reach the agent. Exiting 2 there was
    # the "Stop hook fails with No stderr output" defect.
    if deny_emitted and args.event not in _JSON_DECISION_EVENTS:
        sys.exit(2)


if __name__ == "__main__":
    main()
