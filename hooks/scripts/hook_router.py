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

STATE_DIR = Path(
    os.environ.get(
        "TEATREE_CLAUDE_STATUSLINE_STATE_DIR",
        os.environ.get("T3_HOOK_STATE_DIR", "/tmp/claude-statusline"),  # noqa: S108
    )
)

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
_BLOCKED_COMMANDS: list[tuple[re.Pattern[str], str]] = [
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
            "If `t3` is missing on this machine, install teatree (`uv tool install teatree` "
            "or `uv tool install --editable <teatree-repo>`)."
        ),
    ),
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
# session that is already running the loop. The throttle-and-recreate markers
# (``loop-pending`` / ``pump-armed`` / ``mr_refreshed`` …) are NOT listed: their
# absence is the safe default and they are re-armed on demand.
_SWEEP_PROTECTED_SUFFIXES = frozenset({"crons"})


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

    Returns ``True`` so handlers can ``return emit_pretooluse_deny(...)``.
    """
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
# * with the master ``[teatree] gate_fail_open`` switch ON, every over-deny
#   gate flips to fail-open at once.
#
# The HARD INVARIANT (regression-guarded in test_public_leak_gate_*): the
# PUBLIC-egress leak path (quote/banned on a PUBLIC surface,
# ``publish_surface`` carve-out) MUST NEVER call this helper and MUST NEVER
# read ``gate_fail_open`` — it stays fail-CLOSED always. Relaxing a public
# leak block is a privacy regression, not a lockout rescue.
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


def _gate_fail_open_enabled() -> bool:
    """True iff the master ``[teatree] gate_fail_open`` switch is ON.

    Fails CLOSED to disabled (return ``False``) on any import/resolution
    error so a broken environment never silently relaxes every gate.
    """
    modules = _bootstrap_teatree_src()
    if modules is None:
        return False
    _, teatree_gate = modules
    try:
        return bool(teatree_gate.gate_fail_open_is_enabled())
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
        if _gate_fail_open_enabled():
            return False
    except Exception:  # noqa: BLE001 — a raising resolver must NEVER relax a gate; fail CLOSED to deny.
        return emit_pretooluse_deny(reason)
    return emit_pretooluse_deny(reason)


def _state_file(session_id: str, suffix: str) -> Path:
    return STATE_DIR / f"{session_id}.{suffix}"


def _read_lines(path: Path) -> list[str]:
    if not path.is_file():
        return []
    return [line for line in path.read_text(encoding="utf-8").strip().splitlines() if line]


def _append_line(path: Path, line: str) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(f"{line}\n")


# ── UserPromptSubmit ────────────────────────────────────────────────


def _build_skill_loader_input(prompt: str, session_id: str) -> dict:
    teatree_home = os.environ.get("HOME", "")
    source_root = Path(__file__).resolve().parents[2].parent

    active = _read_lines(_state_file(session_id, "active"))
    loaded = _read_lines(_state_file(session_id, "skills"))

    search_dirs = [str(source_root), f"{teatree_home}/.agents/skills", f"{teatree_home}/.claude/skills"]
    return {
        "prompt": prompt,
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

    # Deterministic t3 CLI reminder — injected when prompt matches
    # workspace/infrastructure patterns, regardless of skill suggestions.
    t3_reminder = _T3_CLI_REMINDER if _T3_CLI_REMINDER_RE.search(prompt) else ""

    if not suggestions:
        if t3_reminder:
            print(t3_reminder)  # noqa: T201
        return

    skill_list = ", ".join(f"/{s}" for s in suggestions)
    pending.write_text("\n".join(suggestions) + "\n", encoding="utf-8")
    parts = [f"LOAD THESE SKILLS NOW (call the Skill tool for each, before doing anything else): {skill_list}."]
    if t3_reminder:
        parts.append(t3_reminder)
    print("\n".join(parts))  # noqa: T201


# ── UserPromptSubmit + PreToolUse: enforce-loop-registration ──────────

_LOOP_CADENCE_DEFAULT = 720
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
    src_dir = Path(__file__).resolve().parents[2] / "src"
    added = False
    try:
        if str(src_dir) not in sys.path:
            sys.path.insert(0, str(src_dir))
            added = True
        from teatree.config import cadence_seconds  # noqa: PLC0415

        return cadence_seconds()
    except Exception:  # noqa: BLE001
        return int(os.environ.get("T3_LOOP_CADENCE", _LOOP_CADENCE_DEFAULT) or _LOOP_CADENCE_DEFAULT)
    finally:
        if added:
            with contextlib.suppress(ValueError):
                sys.path.remove(str(src_dir))


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


def handle_enforce_loop_on_prompt(data: dict) -> None:
    """On first prompt, check if the fat loop needs registration.

    #1295 capability F: emit a structured ``hookSpecificOutput`` directive
    so a harness that natively supports the ``register_cron`` action can
    auto-register without a manual CronCreate. Falls back to the prose
    nag for harnesses that do not consume the structured directive.
    """
    session_id = data.get("session_id", "")
    if not session_id:
        return
    _ensure_state_dir()
    _cleanup_stale_pending(session_id)
    pending = _state_file(session_id, "loop-pending")
    if _session_has_loop(session_id):
        pending.unlink(missing_ok=True)
        return
    if not _tick_meta_stale():
        return
    cadence = _loop_cadence_seconds()
    minutes = max(1, cadence // 60)
    pending.write_text("1", encoding="utf-8")
    # The directive carries the same payload the agent would pass to
    # ``CronCreate`` — a harness consumer reads ``hookSpecificOutput``
    # and skips the prose nag entirely. The prose remains as a fallback
    # for harness builds that do not yet read the directive.
    directive = {
        "hookSpecificOutput": {
            "action": "register_cron",
            "cron": f"*/{minutes} * * * *",
            "prompt": _LOOP_PROMPT,
            "recurring": True,
            "slots": ["tick", "review", "self-improve", "slack-answer"],
        },
    }
    json.dump(directive, sys.stdout)
    print()  # noqa: T201
    print(  # noqa: T201
        f"Session setup: the teatree background loop is not registered yet. "
        f"Please call CronCreate with "
        f'cron="*/{minutes} * * * *", prompt="{_LOOP_PROMPT}", recurring=true.'
    )


def handle_enforce_loop_registration(data: dict) -> bool:
    """Block Bash/Edit/Write until the background loop cron is registered."""
    tool_name = data.get("tool_name", "")
    if tool_name in {"CronCreate", "CronDelete", "CronList", "ScheduleWakeup", "Skill", "ToolSearch"}:
        return False
    session_id = data.get("session_id", "")
    if not session_id:
        return False
    pending = _state_file(session_id, "loop-pending")
    if not pending.is_file():
        return False
    if _session_has_loop(session_id):
        pending.unlink(missing_ok=True)
        return False
    cadence = _loop_cadence_seconds()
    minutes = max(1, cadence // 60)
    reason = (
        f"The teatree background loop is not registered yet. "
        f"Please call CronCreate with "
        f'cron="*/{minutes} * * * *", prompt="{_LOOP_PROMPT}", recurring=true.'
    )
    return emit_pretooluse_deny(reason)


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
        if any((d.parent / name).is_file() or (d / name).is_file() for d in search_dirs):
            return True
        segment = stripped[: -len("/SKILL.md")].rsplit("/", 1)[-1]
    else:
        segment = stripped.rsplit("/", 1)[-1]
    if not segment or segment == "SKILL.md":
        return False
    return any((d / segment / "SKILL.md").is_file() for d in search_dirs)


def handle_enforce_skill_loading(data: dict) -> bool:
    """Block Bash/Edit/Write when *loadable* suggested skills aren't loaded.

    Fails open on a stale/unresolvable required skill (see the module
    comment above): such a name is warned about, never blocked on.
    """
    session_id = data.get("session_id", "")
    if not session_id:
        return False

    pending_lines = _read_lines(_state_file(session_id, "pending"))
    if not pending_lines:
        return False

    loaded = set(_read_lines(_state_file(session_id, "skills")))
    unloaded = [s for s in pending_lines if s not in loaded]
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

    skill_list = " ".join(f"/{s}" for s in enforceable)
    reason = (
        f"SKILL LOADING ENFORCEMENT: You MUST load these skills first: {skill_list}. "
        "Call the Skill tool for each one BEFORE calling Bash/Edit/Write."
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
# The ``TaskCreated`` event DOES fire for the fan-out vehicle (verified
# against the Claude Code 2.1.156 binary: ``hook_event_name:"TaskCreated"``
# with ``task_id``/``task_subject``/``task_description``; a hook output of
# ``{"continue": false, ...}`` sets ``preventContinuation``). This handler
# rides that event to force the matching lifecycle skill + its
# already-transitive companions onto the dispatched task.
#
# It enforces SKILL-LOADING ONLY — it never inspects agent count, token
# budget, ``run_in_background``, or any workflow-size field, so ultracode
# keeps maximal fan-out room. The deny schema is the teammate-stop
# envelope (``{"continue": false, "stopReason": ...}``), NOT the
# ``PreToolUse`` ``hookSpecificOutput`` deny; ``main`` translates the
# handler's ``True`` return into ``sys.exit(2)`` the same as the
# ``PreToolUse`` gates.

# Mandatory reason, mirroring the #1302 ``[skip-plan-gate: <reason>]``
# token: ``[skip-skill-gate: <non-empty-reason>]`` anywhere in the
# subject/description head unblocks the dispatch; an empty reason rejects.
_SKIP_SKILL_GATE_RE = re.compile(r"\[skip-skill-gate:\s*(\S[^\]]*?)\s*\]")


def _skill_loading_gate_enabled() -> bool:
    """Whether the skill-loading-on-task-create gate is enabled (default True).

    Best-effort read of ``[teatree] skill_loading_gate_enabled`` from
    ``~/.teatree.toml``, mirroring :func:`_orchestrator_bash_gate_enabled`'s
    toml-read shape. Fails OPEN to enabled on a missing/broken config so the
    gate keeps its protective default; an explicit ``false`` is the
    one-line kill-switch (never a code edit).
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
    teatree = config.get("teatree") if isinstance(config, dict) else None
    if not isinstance(teatree, dict):
        return True
    return teatree.get("skill_loading_gate_enabled") is not False


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


def _companions_for_task_text(task_text: str) -> list[str]:
    """Resolve the lifecycle skill for *task_text* plus its companion closure.

    Routes the task text through the production
    :meth:`SkillLoadingPolicy.lifecycle_for_task_text` (text → lifecycle
    skill: ``review``/``code``/``ship``/…) and then expands that single
    skill through :func:`resolve_companions` against the real trigger index
    (parsed from real ``SKILL.md`` frontmatter). The companions are already
    transitive — ``review`` pulls in ``code``/``workspace``/``platforms``/
    … — so no companion-resolution logic is rebuilt here. On any
    resolution failure (teatree not importable in this hook process, no
    lifecycle match) the closure is empty and the gate falls back to the
    ``<session>.pending`` demand set alone.
    """
    if not task_text:
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

        from teatree.skill_deps import resolve_companions  # noqa: PLC0415
        from teatree.skill_loading import SkillLoadingPolicy  # noqa: PLC0415

        index = build_trigger_index(_skill_search_dirs())
        lifecycle = SkillLoadingPolicy.lifecycle_for_task_text(task_text, trigger_index=index)
        resolved, _missing = resolve_companions([lifecycle], index) if lifecycle else ([], [])
    except Exception:  # noqa: BLE001
        return []
    else:
        return resolved
    finally:
        for extra in added:
            with contextlib.suppress(ValueError):
                sys.path.remove(extra)


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
    """Force the matching lifecycle skill + companions onto a fanned-out task.

    Demand set = ``(<session>.pending minus <session>.skills)`` union the
    companion closure of ``lifecycle_for_task_text(task_description)``, with each name
    dropped if it does not resolve via :func:`_skill_resolves` (fail-open on
    a renamed/stale skill — this also defuses the auto-loader's observed
    demand for an ``ac-exporting-webhook-mapping`` that errors "Unknown
    skill"). The gate denies only when a RESOLVABLE skill is still unloaded.

    Skill-loading ONLY: no agent-count / token-budget / workflow-size field
    is read, so ultracode keeps maximal fan-out room. Fails open (passes
    through) on the kill-switch, a valid ``[skip-skill-gate: <reason>]``
    token, or a missing session id.
    """
    session_id = data.get("session_id", "")
    if not session_id or not _skill_loading_gate_enabled():
        return False

    subject = data.get("task_subject", "") or ""
    description = data.get("task_description", "") or ""
    if _task_text_skip_token(f"{subject}\n{description}"):
        return False

    loaded = set(_read_lines(_state_file(session_id, "skills")))
    pending = [s for s in _read_lines(_state_file(session_id, "pending")) if s not in loaded]
    detected = [s for s in _companions_for_task_text(description) if s not in loaded]

    demanded: list[str] = []
    for name in [*pending, *detected]:
        if name and name not in demanded:
            demanded.append(name)
    if not demanded:
        return False

    search_dirs = _skill_search_dirs()
    enforceable = [s for s in demanded if _skill_resolves(s, search_dirs)]
    stale = [s for s in demanded if s not in enforceable]

    config_path = os.environ.get("T3_SUPPLEMENTARY_SKILLS", str(Path.home() / ".teatree-skills.yml"))
    for name in stale:
        sys.stderr.write(
            f"WARNING: skill-loading-on-task gate skipped unresolvable skill '{name}' "
            f"(not found in any skill dir; check the keyword→skill mapping in {config_path}).\n"
        )

    if not enforceable:
        return False

    skill_list = " ".join(f"/{s}" for s in enforceable)
    reason = (
        "SKILL LOADING ENFORCEMENT (TaskCreated): this fanned-out task must "
        f"load these teatree skills first: {skill_list}. Call the Skill tool "
        "for each one in the task before doing its work — do NOT run a bespoke "
        "workflow that skips them. (Disable with `t3 <overlay> gate "
        "skill-loading disable` or prefix the task with "
        "`[skip-skill-gate: <reason>]`.)"
    )
    return emit_task_create_deny(reason)


# ── PreToolUse: enforce-plan-gate (#1133) ────────────────────────────
#
# Denies ``Edit``/``Write`` on files under ``$T3_WORKSPACE_DIR`` when the
# current session has neither invoked ``/plan`` nor read the touched file.
# Opt-in per overlay via ``[overlays.<name>] plan_gate = true`` in
# ``~/.teatree.toml`` — default OFF for backward compat. Outside the
# workspace root (``~/.zshrc``, ``~/.claude/``, agent memory) the gate
# never fires.
#
# Session state lives in two STATE_DIR files alongside the existing
# ``<session>.skills`` / ``<session>.reads`` pattern:
#
# - ``<session>.plan-invocations`` — records each ``Skill`` tool call
#   whose ``skill`` field is ``plan`` (or a ``plan*`` variant).
# - ``<session>.workspace-reads`` — records resolved workspace-relative
#   file paths that the agent has ``Read`` this session. Reads outside
#   the workspace are NOT recorded, so an unrelated ``~/.zshrc`` read
#   cannot authorize a workspace ``Edit``.


def _workspace_root() -> Path:
    """Return the absolute ``$T3_WORKSPACE_DIR``, defaulting to ``~/workspace``."""
    return Path(os.environ.get("T3_WORKSPACE_DIR", str(Path.home() / "workspace"))).expanduser().resolve()


def _is_under_workspace(file_path: str) -> bool:
    """True when *file_path* resolves to a path under ``$T3_WORKSPACE_DIR``."""
    if not file_path:
        return False
    try:
        resolved = Path(file_path).expanduser().resolve()
    except (OSError, RuntimeError):
        return False
    try:
        resolved.relative_to(_workspace_root())
    except ValueError:
        return False
    return True


def _plan_gate_enabled() -> bool:
    """True iff any overlay in ``~/.teatree.toml`` has ``plan_gate = true``.

    Mirrors :func:`_load_protected_branches`'s toml-read shape — best-effort
    open, fail closed on parse errors (return ``False`` so the gate stays
    silent rather than spuriously blocking on a broken config).
    """
    import tomllib  # noqa: PLC0415

    config_path = Path.home() / ".teatree.toml"
    if not config_path.is_file():
        return False
    try:
        with config_path.open("rb") as f:
            config = tomllib.load(f)
    except Exception:  # noqa: BLE001
        return False
    return any(overlay_cfg.get("plan_gate") is True for overlay_cfg in (config.get("overlays") or {}).values())


def handle_track_plan_invocation(data: dict) -> None:
    """PostToolUse: record a ``/plan`` invocation for the current session."""
    if data.get("tool_name") != "Skill":
        return
    skill = data.get("tool_input", {}).get("skill", "")
    if not skill:
        return
    # Accept ``plan``, ``t3:plan``, ``plan-*`` — any variant whose final
    # path segment starts with ``plan``.
    final = skill.rsplit(":", 1)[-1].rsplit("/", 1)[-1]
    if not final.startswith("plan"):
        return
    session_id = data.get("session_id", "")
    if not session_id:
        return
    _ensure_state_dir()
    marker = _state_file(session_id, "plan-invocations")
    marker.write_text("1", encoding="utf-8")


def handle_track_workspace_source_read(data: dict) -> None:
    """PostToolUse: record a workspace ``Read`` keyed by the resolved file path."""
    if data.get("tool_name") != "Read":
        return
    file_path = data.get("tool_input", {}).get("file_path", "")
    if not _is_under_workspace(file_path):
        return
    session_id = data.get("session_id", "")
    if not session_id:
        return
    _ensure_state_dir()
    reads_file = _state_file(session_id, "workspace-reads")
    try:
        resolved = str(Path(file_path).expanduser().resolve())
    except (OSError, RuntimeError):
        return
    existing = set(_read_lines(reads_file))
    if resolved not in existing:
        _append_line(reads_file, resolved)


def _session_satisfies_plan_gate(session_id: str, file_path: str) -> bool:
    """True iff *session_id* has recorded a plan invocation OR a read of *file_path*."""
    if _state_file(session_id, "plan-invocations").is_file():
        return True
    try:
        resolved = str(Path(file_path).expanduser().resolve())
    except (OSError, RuntimeError):
        resolved = file_path
    return resolved in set(_read_lines(_state_file(session_id, "workspace-reads")))


def handle_enforce_plan_gate(data: dict) -> bool:
    """Deny ``Edit``/``Write`` under ``$T3_WORKSPACE_DIR`` lacking plan-or-read.

    Returns ``True`` (emits the deny JSON) only when ALL of:

    1. The tool is ``Edit`` or ``Write``.
    2. The target ``file_path`` lives under ``$T3_WORKSPACE_DIR``.
    3. At least one overlay has ``plan_gate = true`` in ``~/.teatree.toml``.
    4. No ``/plan`` invocation has been recorded for this session.
    5. No source-read of the touched file has been recorded for this session.

    Any condition failing -> the handler returns ``False`` (pass through)
    so the surrounding handler chain runs normally.
    """
    tool_name = data.get("tool_name", "")
    file_path = data.get("tool_input", {}).get("file_path", "")
    session_id = data.get("session_id", "")
    if (
        tool_name not in {"Edit", "Write"}
        or not _is_under_workspace(file_path)
        or not _plan_gate_enabled()
        or not session_id
        or _session_satisfies_plan_gate(session_id, file_path)
    ):
        return False

    reason = (
        f"{tool_name} denied on `{file_path}`: file is under $T3_WORKSPACE_DIR "
        "and no `/plan` invocation or source-read for the touched module is "
        "recorded in this session. Run `/plan` first, or Read the touched file "
        "before Edit. (Plan-gate is opt-in per overlay via "
        "`[overlays.<name>] plan_gate = true`.)"
    )
    return emit_pretooluse_deny(reason)


# ── PreToolUse: enforce-agent-plan-gate (#1302) ──────────────────────
#
# Sibling of ``handle_enforce_plan_gate`` (#1133, Edit/Write surface).
# Denies ``Agent`` / ``Task`` dispatch unless one of:
#
# 1. A recent ``/plan`` invocation is recorded in
#    ``$XDG_DATA_HOME/teatree/last-plan-skill-ts`` (default
#    ``~/.local/share/teatree/last-plan-skill-ts``) within the cooldown
#    window (default 30 minutes, configurable via
#    ``TEATREE_PLAN_GATE_WINDOW_MINUTES``; the sentinel ``0`` disables the
#    freshness window entirely, so a single up-front ``/plan`` authorises a
#    big multi-wave ultracode fan-out across many turns without re-planning
#    each wave — #1488).
# 2. The Agent prompt carries an explicit per-call opt-out token
#    ``[skip-plan-gate: <reason>]`` (reason is mandatory — empty rejects).
#
# The marker file is written by ``handle_track_plan_skill_timestamp``
# (PostToolUse), which fires on every ``Skill`` tool call whose final
# path segment starts with ``plan`` (``plan``, ``t3:plan``, ``plan-*``).
# This is wall-clock based rather than per-session so an orchestrator's
# /plan in turn N still authorises sub-agent dispatches across the
# following turns, until the cooldown lapses.
#
# Why the timestamp file (not the existing ``<session>.plan-invocations``
# marker): the existing #1133 gate is per-session and indefinite. The
# Agent-dispatch gate is per-time-window so the "I planned this hour ago"
# proof expires — stale plan assertions are the failure mode the issue
# names.

_AGENT_PLAN_GATE_DEFAULT_WINDOW_MINUTES = 30
_AGENT_PLAN_GATE_TOOLS = {"Agent", "Task"}
# Mandatory reason: ``[skip-plan-gate: <non-empty-reason>]``. We allow
# the token anywhere in the first few hundred characters of the prompt
# so a heading/preamble or a leading "Notes:" block does not force it
# strictly onto line 1.
_SKIP_PLAN_GATE_RE = re.compile(r"\[skip-plan-gate:\s*(\S[^\]]*?)\s*\]")


def _plan_gate_window_minutes() -> int:
    """Resolve the plan-gate freshness window in minutes.

    Returns the default (30) when the env var is unset or unparsable. The
    sentinel ``0`` is honoured verbatim — it disables the freshness window
    so a single up-front ``/plan`` authorises an arbitrarily long
    multi-wave fan-out (#1488). A NEGATIVE value falls back to the default
    (it is meaningless, not a disable signal).
    """
    raw = os.environ.get("TEATREE_PLAN_GATE_WINDOW_MINUTES", "").strip()
    if not raw:
        return _AGENT_PLAN_GATE_DEFAULT_WINDOW_MINUTES
    try:
        value = int(raw)
    except ValueError:
        return _AGENT_PLAN_GATE_DEFAULT_WINDOW_MINUTES
    return value if value >= 0 else _AGENT_PLAN_GATE_DEFAULT_WINDOW_MINUTES


def _plan_skill_timestamp_file() -> Path:
    xdg = os.environ.get("XDG_DATA_HOME", "").strip()
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "teatree" / "last-plan-skill-ts"


def _plan_skill_recently_invoked() -> bool:
    """True iff the gate's timestamp file is fresh within the cooldown window."""
    ts_file = _plan_skill_timestamp_file()
    if not ts_file.is_file():
        return False
    try:
        recorded = int(ts_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    import time  # noqa: PLC0415

    age = int(time.time()) - recorded
    return 0 <= age <= _plan_gate_window_minutes() * 60


def _agent_prompt_skip_token(prompt: str) -> str | None:
    """Return the reason from a ``[skip-plan-gate: <reason>]`` token, else None.

    Scans only the first 512 characters so a buried token in a long
    prompt body does not silently authorise dispatch.
    """
    head = prompt[:512]
    match = _SKIP_PLAN_GATE_RE.search(head)
    if not match:
        return None
    reason = match.group(1).strip()
    return reason or None


def handle_track_plan_skill_timestamp(data: dict) -> None:
    """PostToolUse: write a POSIX timestamp when a ``plan*`` skill is invoked.

    Mirrors the routing of :func:`handle_track_plan_invocation` (final
    path segment starts with ``plan``), but writes a wall-clock marker
    used by the Agent-dispatch plan-gate (#1302) instead of a per-
    session boolean.
    """
    if data.get("tool_name") != "Skill":
        return
    skill = data.get("tool_input", {}).get("skill", "")
    if not skill:
        return
    final = skill.rsplit(":", 1)[-1].rsplit("/", 1)[-1]
    if not final.startswith("plan"):
        return
    import time  # noqa: PLC0415

    ts_file = _plan_skill_timestamp_file()
    ts_file.parent.mkdir(parents=True, exist_ok=True)
    ts_file.write_text(str(int(time.time())), encoding="utf-8")


def handle_enforce_agent_plan_gate(data: dict) -> bool:
    """Deny ``Agent``/``Task`` dispatch lacking a fresh ``/plan`` or a skip token.

    Returns ``True`` (deny JSON emitted) only when ALL of:

    1. The tool is ``Agent`` or ``Task``.
    2. The prompt carries no ``[skip-plan-gate: <reason>]`` token.
    3. The plan-skill timestamp file is missing or older than the
        cooldown window (``TEATREE_PLAN_GATE_WINDOW_MINUTES``, default 30).
    """
    tool_name = data.get("tool_name", "")
    if tool_name not in _AGENT_PLAN_GATE_TOOLS:
        return False

    window = _plan_gate_window_minutes()
    # Sentinel 0 disables the freshness window — a single up-front /plan
    # authorises a big multi-wave ultracode fan-out without re-planning each
    # wave (#1488). The gate then never blocks on staleness.
    if window == 0:
        return False

    prompt = data.get("tool_input", {}).get("prompt", "") or ""
    if _agent_prompt_skip_token(prompt):
        return False
    if _plan_skill_recently_invoked():
        return False

    reason = (
        f"BLOCKED: `{tool_name}` dispatch requires a recent `/plan` invocation "
        f"(within the last {window} minutes) or an explicit per-call opt-out. "
        "Unblock paths: (a) call the Skill tool with skill=`plan` to plan this "
        "dispatch first, or (b) prefix the Agent prompt with "
        "`[skip-plan-gate: <reason>]` (reason mandatory) — e.g. "
        "`[skip-plan-gate: trivial-bug-fix]`. "
        "Override the window via `TEATREE_PLAN_GATE_WINDOW_MINUTES`. (#1302)"
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
    as the out-of-band-merge gate) and ``publish_surface._slug_for_cwd``
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
    src_dir = Path(__file__).resolve().parents[2] / "src"
    added = False
    try:
        if str(src_dir) not in sys.path:
            sys.path.insert(0, str(src_dir))
            added = True
        from teatree.hooks import publish_surface  # noqa: PLC0415

        slug = publish_surface._slug_for_cwd(root_resolved).lower()  # noqa: SLF001
    except Exception:  # noqa: BLE001
        return False
    finally:
        if added:
            with contextlib.suppress(ValueError):
                sys.path.remove(str(src_dir))
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
        "Create a worktree first with `t3 workspace ticket`.",
    )


# ── PreToolUse: validate-mr-metadata ────────────────────────────────


def _extract_mr_fields(data: dict) -> tuple[str, str] | None:
    """Return ``(title, description)`` for an MR create/update, else ``None``.

    ``None`` means "not a `glab mr create/update`" — nothing to validate.
    A returned tuple means the command IS an MR mutation and must be
    validated *even if title/description are empty* — an empty/missing
    title is exactly the kind of bad metadata the pre-push gate must
    reject, not silently pass (#119).
    """
    tool_name = data.get("tool_name", "")
    tool_input = data.get("tool_input", {})

    if tool_name == "Bash":
        command = tool_input.get("command", "")
        if "glab mr create" not in command and "glab mr update" not in command:
            return None
        title_match = re.search(r"""--title\s+['"]([^'"]+)['"]""", command)
        desc_match = re.search(r"""--description\s+['"]([^'"]+)['"]""", command)
        return (title_match.group(1) if title_match else ""), (desc_match.group(1) if desc_match else "")

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
    :func:`_fail_open_or_deny` so the master ``gate_fail_open`` switch and the
    always-allowed self-rescue commands relax it too (NEVER-LOCKOUT).
    """
    if os.environ.get("T3_MR_VALIDATE_ALLOW_BROKEN_ENV", "").strip().lower() in {"1", "true", "yes"}:
        return False
    return _fail_open_or_deny(data, _MR_VALIDATE_BROKEN_ENV_DENY)


def _run_mr_validator(argv: list[str], title: str, description: str) -> "subprocess.CompletedProcess[str] | None":
    """Run the validator, or ``None`` if the env is broken (timeout/missing)."""
    try:
        return subprocess.run(  # noqa: S603
            [*argv, "--title", title, "--description", description],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None


def handle_validate_mr_metadata(data: dict) -> bool:
    """Block a non-compliant ``glab mr create/update`` before it runs.

    Validates by default via the active overlay's ``validate_pr`` (no
    env-var opt-in) so the pre-push gate is always live (#119 Part 3). When
    the validator cannot be resolved or crashes, the gate FAILS CLOSED — a
    non-compliant title must never slip onto GitLab on a broken env. The
    explicit ``T3_MR_VALIDATE_ALLOW_BROKEN_ENV`` opt-in restores fail-open as
    a deliberate self-rescue.
    """
    fields = _extract_mr_fields(data)
    if fields is None:
        return False
    title, description = fields

    argv = _mr_validate_argv()
    if argv is None:
        return _handle_broken_validate_env(data)

    result = _run_mr_validator(argv, title, description)
    if result is None:
        return _handle_broken_validate_env(data)

    if result.returncode != 0:
        return emit_pretooluse_deny(
            (result.stderr or result.stdout or "").strip() or "MR title/description failed overlay validation."
        )
    return False


# ── PreToolUse: block-ai-signature (#836 §17.6 gate 15) ─────────────

_PR_BODY_FLAG_RE = re.compile(r"--(?:body|description|message)(?:[ =])\s*(['\"])(.*?)\1", re.DOTALL)
_GIT_COMMIT_M_RE = re.compile(r"git\s+commit\b[^\n]*?-m\s+(['\"])(.*?)\1", re.DOTALL)
# File-based message args — the standard multi-line path (#831's shape).
# ``git commit -F/-C/--file FILE``, ``gh pr create --body-file FILE``,
# ``glab mr create --description FILE``. The captured token is a path
# (optionally quoted); a missing/binary file fails open (no scan, no
# crash) — see ``_read_message_file``.
#
# Long flags require a space or ``=`` separator (``--body-file FILE`` /
# ``--body-file=FILE``). Short flags additionally accept the GLUED form
# git's getopt permits — ``git commit -F<path>`` / ``-C<path>`` with no
# separator at all (the residual #862 cold-review found: a glued short
# flag carrying a banned trailer slipped past the space/``=``-only
# matcher). ``[ =]*`` on the short-flag branch covers glued, space, and
# ``=`` uniformly.
_MSG_FILE_FLAG_RE = re.compile(
    r"(?:(?:--body-file|--file|--description)[ =]+|-[FC][ =]*)['\"]?([^'\"\s]+)['\"]?",
)
_PR_CREATE_TOOLS = {
    "mcp__glab__glab_mr_create",
    "mcp__glab__glab_mr_update",
    "mcp__github__create_pull_request",
    "mcp__github__update_pull_request",
}


def _read_message_file(command: str) -> str | None:
    """Read a file-based message arg (``-F``/``--body-file``/etc.).

    The standard multi-line path is exactly #831's shape. A
    missing/unreadable/binary file fails open (returns ``None``: no
    scan, no crash) — matching the other t3-shelling hooks' posture of
    never blocking the agent on a broken environment.
    """
    match = _MSG_FILE_FLAG_RE.search(command)
    if match is None:
        return None
    path = Path(match.group(1))
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _extract_ai_sig_payload(data: dict) -> str | None:
    """Return the PR-body / commit-message text to scan, else ``None``.

    Covers ``gh pr create --body``, ``glab mr create/update
    --description``, ``git commit -m`` (inline), the file-based message
    path (``git commit -F/-C``, ``gh pr create --body-file``, ``glab mr
    create --description <file>`` — the standard multi-line / #831
    shape), and the MR/PR MCP create/update tools. ``None`` ⇒ not a
    PR/commit mutation, or a file-based arg whose file is
    missing/binary (fail open).
    """
    tool_name = data.get("tool_name", "")
    tool_input = data.get("tool_input", {})

    if tool_name == "Bash":
        command = tool_input.get("command", "")
        pr_cmds = ("gh pr create", "gh pr edit", "glab mr create", "glab mr update")
        is_pr = any(c in command for c in pr_cmds)
        is_commit = "git commit" in command
        if not (is_pr or is_commit):
            return None
        # Inline message wins when present; otherwise fall back to the
        # file-based arg (the multi-line path #831 actually used).
        inline = _PR_BODY_FLAG_RE.search(command) if is_pr else _GIT_COMMIT_M_RE.search(command)
        if inline is not None:
            return inline.group(2)
        from_file = _read_message_file(command)
        if from_file is not None:
            return from_file
        # No scannable payload found. A PR command with neither inline
        # body nor a readable file is treated as nothing-to-scan ('');
        # a commit with no -m and no file opens an editor (None).
        return "" if is_pr else None

    if tool_name in _PR_CREATE_TOOLS:
        return tool_input.get("body", "") or tool_input.get("description", "")

    return None


def _ai_sig_scan_argv() -> list[str] | None:
    t3_bin = shutil.which("t3")
    if t3_bin:
        return [t3_bin, "tool", "ai-sig-scan", "-"]
    return None


def handle_block_ai_signature(data: dict) -> bool:
    """Refuse a PR body / commit message carrying an AI-signature trailer.

    Deterministic enforcement of the "No AI Signature on Posts Made on the
    User's Behalf" rule (BLUEPRINT §17.6 gate 15, #836). The rule was prose
    only in /t3:rules and unenforced at the PR-body layer — PR #831 leaked
    the banned trailer, caught only by cold review. This makes it a code
    gate at the same pre-merge layer as the draft-lock and structured-
    question gates. Fails open on a broken environment (no ``t3``), matching
    the other t3-shelling hooks.
    """
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

    if result.returncode != 0:
        return emit_pretooluse_deny(
            "BLOCKED: AI-signature / banned trailer in the PR body or commit message. "
            "Remove it before creating the PR/commit (BLUEPRINT §17.6 gate 15).\n"
            + (result.stdout or result.stderr or "").strip()
        )
    return False


# ── PreToolUse: pre-publish quote-scanner gate (#1213) ──────────────


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
    """
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
    mandatory), mirroring the ``[skip-plan-gate: <reason>]`` convention —
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


# ── PreToolUse: banned-terms posting gate (#1415) ───────────────────


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


def _run_banned_terms_pretool(data: dict) -> bool:
    """Banned-terms inner body — assumes ``teatree`` is already importable."""
    from typing import cast  # noqa: PLC0415

    from teatree.hooks import banned_terms_scanner, publish_surface  # noqa: PLC0415

    tool_name = data.get("tool_name", "")
    raw_input = data.get("tool_input", {}) or {}
    if not isinstance(raw_input, dict):
        return False
    tool_input = cast("banned_terms_scanner.ToolInput", raw_input)

    payload = banned_terms_scanner.extract_publish_payload(tool_name, tool_input)
    if payload is None:
        return False

    if banned_terms_scanner.has_override(tool_name, tool_input):
        return False

    term = banned_terms_scanner.scan_text(payload)
    if term is None:
        return False

    command = tool_input.get("command", "")
    if publish_surface.carve_out_applies(tool_name, command, payload, _resolve_cwd_repo(data)):
        sys.stderr.write(
            f"WARNING: banned-terms gate (#1415) — term '{term}' on a private-repo commit; "
            "downgraded to warn (#126). The repo's own domain words are expected on its commits.\n"
        )
        return False

    return emit_pretooluse_deny(banned_terms_scanner.format_block_message(term))


# ── PreToolUse: bare-reference link gate (#1530) ────────────────────


def handle_bare_reference_pretool(data: dict) -> bool:
    """Refuse a publish whose body cites a bare reference instead of a link.

    Sibling of the #1213 quote-scanner and #1415 banned-terms gates.
    Promotes the prose-only "always a clickable link, never a bare id"
    rule (``feedback_always_clickable_links_never_bare_ids.md``) to a
    deterministic pre-publish gate. Reuses the shared #1213
    ``_command_parser`` publish-surface detection + body extraction, then
    matches the extracted body against the bare-reference catalogue.

    A bare ``#NNNN`` / ``!NNNN`` / Slack ``ts`` / forge-or-Notion URL not
    wrapped in a clickable link ⇒ refuse via ``permissionDecision: deny``
    + a reason naming each offending ref. Outgoing surfaces only
    (gh/glab/git-commit/t3-notify/slack-send) — never internal reads.

    Fail-open on any internal error: a crashing hook is worse than no
    scan. The handler bootstraps ``sys.path`` to import ``teatree`` from
    the sibling ``src/`` directory (#1314) and swallows any exception,
    returning ``False`` so the tool use proceeds unchanged.
    """
    src_dir = Path(__file__).resolve().parents[2] / "src"
    added = False
    try:
        if str(src_dir) not in sys.path:
            sys.path.insert(0, str(src_dir))
            added = True
        return _run_bare_reference_pretool(data)
    except Exception:  # noqa: BLE001
        return False
    finally:
        if added:
            with contextlib.suppress(ValueError):
                sys.path.remove(str(src_dir))


def _run_bare_reference_pretool(data: dict) -> bool:
    """Bare-reference inner body — assumes ``teatree`` is already importable."""
    from typing import cast  # noqa: PLC0415

    from teatree.hooks import bare_reference_scanner  # noqa: PLC0415

    tool_name = data.get("tool_name", "")
    raw_input = data.get("tool_input", {}) or {}
    if not isinstance(raw_input, dict):
        return False
    tool_input = cast("bare_reference_scanner.ToolInput", raw_input)

    payload = bare_reference_scanner.extract_publish_payload(tool_name, tool_input)
    if payload is None:
        return False

    refs = bare_reference_scanner.scan_text(payload)
    if not refs:
        return False

    return emit_pretooluse_deny(bare_reference_scanner.format_block_message(refs))


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
    ``glab mr create``. ``gh pr ready --undo`` (return-to-draft, the
    gate's own remediation) and ``--draft`` creation are excluded.
    """
    if data.get("tool_name") != "Bash":
        return False
    command = data.get("tool_input", {}).get("command", "")
    if _GH_PR_READY_RE.search(command) or _PR_MR_CREATE_RE.search(command):
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
# HEAVY / long-running Bash shapes the main agent should not run inline.
# This is a HEURISTIC denylist (anchored, case-sensitive on the verb);
# the escape hatch is ``run_in_background: true`` (or, for a whole class
# of work, dispatching a sub-agent). When in doubt the command is
# ALLOWED — only an explicit match here, foreground, is gated. Patterns
# cover: Python/test runners, language/asset builds, dev servers,
# browser E2E, package installs/sync, long sleeps, and full-tree
# recursive sweeps (the shapes that actually wedge a session).
_ORCHESTRATOR_HEAVY_BASH_RE = re.compile(
    r"(?:"
    r"\bpytest\b|"
    r"\btox\b|"
    r"\bt3\s+\S+\s+(?:run|e2e|test)\b|"
    r"manage\.py\s+runserver|"
    r"\bnx\s+(?:serve|run)\b|"
    r"docker\s+compose\s+(?:up|build)|"
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
    r"\bls\s+-[a-zA-Z]*R\b"
    r")",
)


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

    Best-effort read of ``[teatree] orchestrator_bash_gate_enabled`` from
    ``~/.teatree.toml``, mirroring :func:`_plan_gate_enabled`'s toml-read
    shape. Fails OPEN to enabled on a missing/broken config so the gate
    keeps its protective default; an explicit ``false`` is the kill-switch
    that lets the user disable it with one config line (never a code
    edit).
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
    teatree = config.get("teatree") if isinstance(config, dict) else None
    if not isinstance(teatree, dict):
        return True
    return teatree.get("orchestrator_bash_gate_enabled") is not False


def _deny_foreground_agent_dispatch(data: dict) -> bool:
    """#1442: deny a main-agent foreground ``Agent`` dispatch.

    A foreground dispatch blocks the orchestrator for the entire
    sub-agent runtime (often 30+ min) — a recurring failure (memory rule
    ``feedback_always_run_in_background_for_sub_agent_dispatch``). Only
    the main agent is governed; a sub-agent dispatching its own ``Agent``
    may pick foreground.
    """
    if _call_is_from_subagent(data) or data.get("tool_input", {}).get("run_in_background") is True:
        return False
    return emit_pretooluse_deny(
        "[main-agent-orchestration-guard] Foreground Agent dispatch "
        "DENIED in main agent context.\n"
        "Pass `run_in_background: true` to every Agent invocation "
        "from the main agent.\n"
        "Memory rule: "
        "feedback_always_run_in_background_for_sub_agent_dispatch "
        "(RED CARD recurrence)."
    )


def _deny_heavy_main_agent_bash(data: dict) -> bool:
    """Deny a main-agent foreground HEAVY/long-running ``Bash`` command.

    Passes through when the call is a sanctioned orchestration verb,
    comes from a sub-agent, is dispatched with ``run_in_background:
    true``, or does not match the heavy denylist
    (:data:`_ORCHESTRATOR_HEAVY_BASH_RE`).
    """
    if _is_orchestration_action(data) or _call_is_from_subagent(data):
        return False
    tool_input = data.get("tool_input", {})
    if tool_input.get("run_in_background") is True:
        return False
    command = tool_input.get("command", "")
    if not _ORCHESTRATOR_HEAVY_BASH_RE.search(command):
        return False
    return emit_pretooluse_deny(
        "BLOCKED: the orchestrator (main agent) ran a command that looks "
        "long-running / heavy and would tie up this session: "
        f"`{command[:120]}`.\n"
        "The orchestrator is delegate-only for heavy work (BLUEPRINT "
        "§17.4 / §17.8 / §17.6 gate 2). Either pass `run_in_background: "
        "true` to run it without blocking the session, dispatch a "
        "sub-agent (Task/Agent) to do it, or — if this is a false "
        "positive — set `orchestrator_bash_gate_enabled = false` under "
        "`[teatree]` in ~/.teatree.toml to disable the gate."
    )


def handle_enforce_orchestrator_boundary(data: dict) -> bool:
    """Flag the MAIN agent running a HEAVY/long-running Bash command.

    Deterministic enforcement of the orchestrator-decides /
    loop-executes topology (BLUEPRINT §17.4 / §17.8 / §17.6 gate 2): the
    orchestrator keeps its session responsive by delegating long work.
    When the main agent (not a sub-agent — see
    :func:`_call_is_from_subagent`) runs a foreground Bash command that
    matches the heavy denylist (:data:`_ORCHESTRATOR_HEAVY_BASH_RE`) and
    is not dispatched with ``run_in_background: true``, the call is
    blocked with an actionable message. Everything else — quick
    orientation Bash, ``git`` reads/commits, ``cat``/``ls``/``grep`` —
    passes. Sub-agents are unaffected: they are the hands that implement
    and may run any command, heavy or not. The ``Agent`` foreground guard
    (#1442) rides the same handler.

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
    the real :func:`teatree.skill_deps.resolve_requires` resolver — a loaded
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

        from teatree.skill_deps import resolve_requires  # noqa: PLC0415

        index = build_trigger_index(_skill_search_dirs())
        return resolve_requires(skills, index)
    except Exception:  # noqa: BLE001
        return list(skills)
    finally:
        for extra in added:
            with contextlib.suppress(ValueError):
                sys.path.remove(extra)


def _record_skills(skills_file: Path, existing: set[str], skills: list[str]) -> None:
    """Append the resolved closure of *skills*, preserving order, deduped."""
    for name in _resolve_skill_closure(skills):
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
    existing = set(_read_lines(skills_file))

    # PostToolUse: single skill from tool_input
    skill_name = data.get("tool_input", {}).get("skill", "")
    if skill_name:
        _record_skills(skills_file, existing, [skill_name])
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
    _record_skills(skills_file, existing, loaded)


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


# ── PostToolUse: capture TodoWrite into durable per-session state ──
#
# Issue #970: the recovery snapshot was missing the active TODO list.
# When ``TodoWrite`` fires, persist the latest todos to
# ``<session>.todos`` so the PreCompact snapshot can quote them back.
# Anthropic's newer ``TaskCreate``/``TaskUpdate`` tools currently bypass
# PostToolUse (documented regression in ``docs/claude-code-internals.md``);
# whenever they start firing hooks again, register them here too.


def handle_track_todos(data: dict) -> None:
    """Persist the current ``TodoWrite`` todo list to ``<session>.todos``.

    Stores one ``- [status] content`` line per todo so the snapshot
    renderer can include it verbatim. Overwrites on each TodoWrite so
    completed/removed items don't linger — TodoWrite is the source of
    truth for the active list. No-op for any other tool name.
    """
    if data.get("tool_name") != "TodoWrite":
        return
    session_id = data.get("session_id", "")
    if not session_id:
        return
    todos = data.get("tool_input", {}).get("todos", [])
    if not isinstance(todos, list):
        return

    _ensure_state_dir()
    todos_file = _state_file(session_id, "todos")
    lines: list[str] = []
    for todo in todos:
        if not isinstance(todo, dict):
            continue
        content = str(todo.get("content", "")).strip()
        if not content:
            continue
        status = str(todo.get("status", "pending")).strip() or "pending"
        lines.append(f"- [{status}] {content}")
    todos_file.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


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
    blocking). Captured TODOs (via :func:`handle_track_todos`) round out
    "what was I about to do next" from the durable side, since the Tasks
    API doesn't fire ``PostToolUse``.
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

    todos = _read_lines(_state_file(session_id, "todos"))
    if todos:
        lines += ["", "## Pending TODOs", *todos]

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
    lazy ``teatree.skill_deps`` import elsewhere in this module).

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


def _evict_stale_db_lease_owner(session_id: str) -> None:
    """Orphan any ``LoopLease`` ``loop-owner`` row not held by ``session_id``.

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

    Compaction is the natural eviction point. Whenever the SessionStart
    handler records a new session as the tick-owner, any stale DB row
    (session_id != new id, including the empty-string baseline) is
    orphaned so the new session's next tick CAS-claims it cleanly. The
    lease stays the authoritative liveness source; the registry is the
    discovery channel.

    Best-effort: any Django bootstrap / DB error fails open. The hook
    must never block the SessionStart directive over a DB hiccup.
    """
    if not _bootstrap_teatree_django():
        return
    try:
        from teatree.core.models import LoopLease  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return
    try:
        LoopLease.objects.filter(name="loop-owner").exclude(session_id=session_id).update(
            session_id="",
            acquired_at=None,
            lease_expires_at=None,
        )
    except Exception:  # noqa: BLE001
        return


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
    """
    session_id = data.get("session_id", "")
    if not session_id:
        return
    agent_id = data.get("agent_id", "")

    became_owner_after_rotation = False
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
        else:
            # No live owner, or this session already owns it (incl. the
            # post-compaction same-session restart — nothing to re-spawn,
            # the cron keeps ticking). This session is the tick-owner.
            # ``owner is None`` here = a fresh machine OR a dead-owner
            # prune; both can leave a stale DB lease behind. The
            # ``same-session refresh`` case (``owner.session_id ==
            # session_id``) does not need eviction — gating on this flag
            # spares those SessionStarts the Django startup cost.
            became_owner_after_rotation = owner is None
            box[0] = _tick_owner_record(session_id, owner.get("agent_id", "") if owner else agent_id or "")
            context = _TICK_DISPATCH_OWNER_DIRECTIVE
            emit_osc = True

    # #1380: orphan any stale DB ``loop-owner`` row from a rotated session
    # (compaction rotated the Claude ``session_id``, leaving the DB lease
    # under the previous id with an unexpired ``lease_expires_at``). The
    # lease stays the authoritative liveness source; this aligns it with
    # the file registry we just rewrote so the new session's next
    # ``t3 loop tick`` CAS-claims cleanly without ``--take-over``. Outside
    # the flock — the DB has its own CAS serialization; holding the
    # registry flock across a Django bootstrap would needlessly stall
    # sibling SessionStart hooks.
    if became_owner_after_rotation:
        _evict_stale_db_lease_owner(session_id)

    # OSC write is a tty side effect, not registry state — keep it out of
    # the flock critical section.
    if emit_osc:
        _emit_osc_title()

    # #845: SessionStart with source=="compact" is the ONLY post-compaction
    # event whose additionalContext the harness reads. Merge the recovered
    # snapshot into this single stdout write (a second chained handler
    # writing JSON would emit invalid concatenated JSON on stdout).
    if data.get("source") == "compact":
        recovered = _recover_snapshot_context(session_id)
        if recovered is not None:
            context = f"{recovered}\n\n---\n\n{context}"

    # #980: surface the harness auto-compact kill-switch advisory when the
    # env-var combo would silently disable auto-compaction on this session.
    # Same single stdout write — see the #845 note above.
    advisory = _autocompact_kill_switch_advisory()
    if advisory:
        context = f"{context}\n\n---\n\n{advisory}"

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
    """Return the loop's pending-spawn work via ``t3 loop pending-spawn --json``.

    Pure read of the existing dispatch seam — no new state. ``[]`` on any
    failure (no ``t3``, timeout, non-zero, malformed) so the self-pump
    fails safe to "idle" rather than spinning on a broken read.
    """
    t3_bin = shutil.which("t3")
    if not t3_bin:
        return []
    try:
        result = subprocess.run(  # noqa: S603
            [t3_bin, "loop", "pending-spawn", "--json"],
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

    Immediate mitigation knob: ``T3_LOOP_DISOWN`` truthy in the
    session's env makes even the owner's Stop hook a clean no-op, so a
    session can stop driving the loop in-process without touching the
    registry or ending the session.
    """
    if os.environ.get("T3_LOOP_DISOWN", "").strip() not in _DISOWN_FALSEY:
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
    reason = (
        "TEATREE LOOP SELF-PUMP — consolidated work remains; continue the loop "
        "without waiting for an external prompt. Run `t3 loop tick`, then "
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

_USER_DIRECTED_CUE_RE = re.compile(
    r"\b("
    r"want me to|should i|shall i|do you want|do you|would you like|"
    r"which (?:one|approach|option|of)|"
    r"prefer|proceed\?|go ahead\?"
    r")\b|\bor\b[^.?!\n]*\?",
    re.IGNORECASE,
)

# A "soft ask" — a deferral phrasing that solicits a user decision WITHOUT
# a literal '?'. "Let me know if/whether …" reads as a status footnote in
# a loop run yet is exactly the lost-decision failure mode #807 targets,
# so it trips the gate independently of the '?' requirement.
_SOFT_ASK_CUE_RE = re.compile(r"\blet me know (?:if|whether|which|what)\b", re.IGNORECASE)

_FENCED_CODE_RE = re.compile(r"```.*?```", re.DOTALL)


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


def _is_user_directed_question(text: str) -> bool:
    """True when ``text`` poses a decision question directed at the user.

    Fenced code blocks are stripped first so a ``?`` inside a regex or
    shell glob is not mistaken for a prompt. A "soft ask" ("let me know
    if/whether …") trips the gate on its own — it solicits a decision
    without a ``?``. Otherwise a ``?`` is necessary but not sufficient: a
    second-person/decision cue must also be present, which keeps
    rhetorical asides and explanatory sentences out of the gate.
    """
    prose = _FENCED_CODE_RE.sub(" ", text)
    if _SOFT_ASK_CUE_RE.search(prose):
        return True
    if "?" not in prose:
        return False
    return bool(_USER_DIRECTED_CUE_RE.search(prose))


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
    return bool(_CLASSIFIER_RELAX_MARKERS.search(_FENCED_CODE_RE.sub(" ", text)))


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
    """
    if data.get("stop_hook_active"):
        return None
    turn = _last_assistant_turn(data.get("transcript_path", ""))
    if turn is None:
        return None
    text, used_question_tool = turn
    if used_question_tool or not _is_user_directed_question(text):
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

    # 1. Canonical teatree loop prompt → stable name (it runs `t3 loop tick`).
    if prompt == _LOOP_PROMPT or prompt.startswith(_LOOP_PROMPT):
        return "tick"

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


def _deny_match(command: str) -> str | None:
    """Return a deny reason for *command*, or None if it should pass through."""
    # Checked FIRST — even before t3/read-only bypass — because agents must
    # never opt in to remote pg_dump regardless of the surrounding command.
    if _REMOTE_DUMP_ENV_RE.search(command):
        return _REMOTE_DUMP_DENY_REASON
    stripped = command.lstrip()
    if _T3_CMD_PREFIX_RE.match(stripped) or _READONLY_CMD_PREFIX_RE.match(stripped):
        return None
    for pattern, reason in _BLOCKED_COMMANDS:
        if pattern.search(command):
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
_OUT_OF_BAND_MERGE_REASON = (
    "BLOCKED: raw `gh pr merge` / `glab mr merge` on a teatree-managed repo — "
    "an out-of-band merge bypasses the FSM coherence mechanism (ledger update, "
    "MergeClear validation, SHA-binding, privacy/AI-signature scan, mark_merged). "
    "Use the sanctioned keystone transition `t3 <overlay> ticket merge <clear_id>` "
    "(BLUEPRINT §17.1 invariant 8 / §17.4). If this repo is genuinely not "
    "teatree-managed and the cwd could not be resolved, run the merge from inside "
    "the repo's working tree so the gate can classify it."
)


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


def _cwd_is_teatree_managed(cwd: Path) -> bool | None:
    """Whether *cwd* belongs to a teatree-managed repo.

    Returns ``True`` (managed — keep the keystone-merge block), ``False``
    (unmanaged — allow a raw merge), or ``None`` (cannot classify — the
    caller fails safe and BLOCKS). Reuses ``publish_surface._slug_for_cwd``
    for slug resolution so the host/owner/repo shape matches the
    private-repo carve-out's.
    """
    slugs, paths = _overlay_managed_repo_signals()
    for base in paths:
        with contextlib.suppress(OSError, RuntimeError):
            cwd.resolve().relative_to(base)
            return True
    src_dir = Path(__file__).resolve().parents[2] / "src"
    added = False
    try:
        if str(src_dir) not in sys.path:
            sys.path.insert(0, str(src_dir))
            added = True
        from teatree.hooks import publish_surface  # noqa: PLC0415

        slug = publish_surface._slug_for_cwd(cwd).lower()  # noqa: SLF001
    except Exception:  # noqa: BLE001
        return None
    finally:
        if added:
            with contextlib.suppress(ValueError):
                sys.path.remove(str(src_dir))
    if not slug:
        return None
    return any(entry in slug for entry in slugs)


def handle_block_out_of_band_merge(data: dict) -> bool:
    """Block a raw ``gh pr merge`` / ``glab mr merge`` on a managed repo.

    Carve-out for the permanent-lockout case (#126): a merge is allowed only
    when the cwd repo is confidently NOT teatree-managed. Managed repos and
    any case the gate cannot classify stay BLOCKED — fail-safe on uncertainty.
    """
    if data.get("tool_name") != "Bash":
        return False
    command = data.get("tool_input", {}).get("command", "")
    if not command or not _OUT_OF_BAND_MERGE_RE.search(command):
        return False
    cwd = _resolve_cwd_repo(data)
    if cwd is None:
        return emit_pretooluse_deny(_OUT_OF_BAND_MERGE_REASON)
    managed = _cwd_is_teatree_managed(cwd)
    if managed is False:
        return False
    return emit_pretooluse_deny(_OUT_OF_BAND_MERGE_REASON)


# ── PreToolUse: mirror-question-to-slack ─────────────────────────────


def _slack_config_from_toml() -> tuple[str, str] | None:
    """Return (bot_token_ref, user_id) from the first slack-enabled overlay."""
    import tomllib  # noqa: PLC0415

    config_path = Path.home() / ".teatree.toml"
    if not config_path.is_file():
        return None
    try:
        with config_path.open("rb") as f:
            config = tomllib.load(f)
    except Exception:  # noqa: BLE001
        return None
    for overlay_cfg in (config.get("overlays") or {}).values():
        if overlay_cfg.get("messaging_backend") == "slack":
            ref = overlay_cfg.get("slack_token_ref", "")
            uid = overlay_cfg.get("slack_user_id", "")
            if ref and uid:
                return ref, uid
    return None


def _format_question_text(questions: list[dict]) -> str:
    lines: list[str] = []
    for q in questions:
        lines.append(f"*{q.get('question', '')}*")
        for i, opt in enumerate(q.get("options", []), 1):
            label = opt.get("label", "")
            desc = opt.get("description", "")
            lines.append(f"  {i}. {label}" + (f" — {desc}" if desc else ""))
    lines.append("\n_Reply with the number (e.g. `1`) or type your answer._")
    return "\n".join(lines)


def _slack_dm_cache_path() -> Path:
    base = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share")))
    return base / "teatree" / "slack_dm_channels.json"


def _read_dm_channel_cache(user_id: str) -> str:
    path = _slack_dm_cache_path()
    if not path.is_file():
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(data, dict):
        return ""
    cached = data.get(user_id)
    return cached if isinstance(cached, str) else ""


def _write_dm_channel_cache(user_id: str, channel: str) -> None:
    path = _slack_dm_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        existing = {}
    if not isinstance(existing, dict):
        existing = {}
    existing[user_id] = channel
    path.write_text(json.dumps(existing, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _slack_open_dm(bot_token: str, user_id: str, *, timeout: float) -> str:
    import urllib.request  # noqa: PLC0415

    payload = json.dumps({"users": user_id}).encode()
    req = urllib.request.Request(
        "https://slack.com/api/conversations.open",
        data=payload,
        headers={"Authorization": f"Bearer {bot_token}", "Content-Type": "application/json"},
    )
    try:
        resp = json.loads(urllib.request.urlopen(req, timeout=timeout).read())  # noqa: S310
    except Exception:  # noqa: BLE001
        return ""
    if not isinstance(resp, dict):
        return ""
    channel = resp.get("channel")
    if not isinstance(channel, dict):
        return ""
    cid = channel.get("id")
    return cid if isinstance(cid, str) else ""


def _slack_post_message(bot_token: str, channel: str, text: str, *, timeout: float) -> bool:
    """Post ``text`` to ``channel``. Return True iff Slack acknowledged success."""
    import urllib.request  # noqa: PLC0415

    payload = json.dumps({"channel": channel, "text": text}).encode()
    req = urllib.request.Request(
        "https://slack.com/api/chat.postMessage",
        data=payload,
        headers={"Authorization": f"Bearer {bot_token}", "Content-Type": "application/json"},
    )
    try:
        resp = json.loads(urllib.request.urlopen(req, timeout=timeout).read())  # noqa: S310
    except Exception:  # noqa: BLE001
        return False
    return bool(isinstance(resp, dict) and resp.get("ok") is True)


def _slack_post_dm(bot_token: str, user_id: str, text: str, *, timeout: float = 2.0) -> None:
    """Post ``text`` to ``user_id``'s DM. Resolves channel via cache when possible.

    Cache hit → single ``chat.postMessage`` call (sub-second on a normal
    connection, fits inside the 3s hook timeout). Cache miss or
    ``channel_not_found`` → open the DM, cache the channel id, retry.
    """
    cached = _read_dm_channel_cache(user_id)
    if cached and _slack_post_message(bot_token, cached, text, timeout=timeout):
        return
    channel = _slack_open_dm(bot_token, user_id, timeout=timeout)
    if not channel:
        return
    if _slack_post_message(bot_token, channel, text, timeout=timeout):
        _write_dm_channel_cache(user_id, channel)


def _perform_slack_post(slack_cfg: tuple[str, str], questions: list[dict]) -> None:
    """Resolve the bot token and post the question — runs synchronously.

    Synchronous so the Slack DM lands **before** the AskUserQuestion prompt
    renders in the terminal. The previous fork-and-detach variant caused
    the message to arrive *after* the user had already answered.
    """
    token_ref, user_id = slack_cfg
    result = subprocess.run(  # noqa: S603
        ["pass", "show", f"{token_ref}-bot"],  # noqa: S607
        capture_output=True,
        text=True,
        timeout=2,
        check=False,
    )
    bot_token = result.stdout.strip() if result.returncode == 0 else ""
    if not bot_token:
        return
    _slack_post_dm(bot_token, user_id, _format_question_text(questions))


def _post_question_to_slack(data: dict) -> None:
    questions = data.get("tool_input", {}).get("questions", [])
    if not questions:
        return
    slack_cfg = _slack_config_from_toml()
    if slack_cfg is None:
        return
    _perform_slack_post(slack_cfg, questions)


def handle_mirror_question_to_slack(data: dict) -> bool:
    if data.get("tool_name") != "AskUserQuestion":
        return False
    _post_question_to_slack(data)
    return False


# ── PreToolUse: route-away-mode-question (#58, BLUEPRINT §17.1 invariant 9) ────


def _bootstrap_teatree_django() -> bool:
    """Import teatree and run ``django.setup()`` once per hook process.

    Returns ``True`` when the bootstrap succeeded (the away-mode handler
    can record a ``DeferredQuestion`` row) and ``False`` when ``teatree``
    is unavailable (the handler then fails open — never intercepts).
    """
    src_dir = Path(__file__).resolve().parents[2] / "src"
    if str(src_dir) not in sys.path:
        sys.path.insert(0, str(src_dir))
    try:
        import django  # noqa: PLC0415

        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "teatree.settings")
        django.setup()
    except Exception:  # noqa: BLE001
        return False
    return True


def _record_deferred_question(question_text: str, options: list[dict], data: dict) -> int | None:
    """Record one ``DeferredQuestion`` row from the ``AskUserQuestion`` payload."""
    if not _bootstrap_teatree_django():
        return None
    try:
        from teatree.core.models.deferred_question import DeferredQuestion  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return None
    try:
        row = DeferredQuestion.record(
            question_text,
            options_json=json.dumps(options) if options else "",
            session_id=str(data.get("session_id", "")),
            tool_use_id=str(data.get("tool_use_id", "")),
        )
    except Exception:  # noqa: BLE001
        return None
    return int(row.pk)


def _resolved_away_mode() -> bool:
    """Resolve the effective availability mode; True when ``away``."""
    if not _bootstrap_teatree_django():
        return False
    try:
        from teatree.core.availability import MODE_AWAY, resolve_mode  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return False
    try:
        return resolve_mode().mode == MODE_AWAY
    except Exception:  # noqa: BLE001
        return False


def handle_route_away_mode_question(data: dict) -> bool:
    """Convert an ``AskUserQuestion`` to a ``DeferredQuestion`` when availability=away.

    Runs FIRST in the PreToolUse chain for ``AskUserQuestion`` so the
    routing decision precedes the Slack mirror (the colleague should
    not be paged for a question the agent already converted). Returns
    ``True`` with a ``permissionDecision=deny`` and a friendly reason
    that names the recorded row so the agent narrates the conversion
    correctly. The denied tool_use block still appears in the transcript,
    so the §807 structured-question Stop gate ``_last_assistant_turn``
    detects ``used_question_tool=True`` and lets the turn complete.
    """
    if data.get("tool_name") != "AskUserQuestion":
        return False
    if not _resolved_away_mode():
        return False
    questions = data.get("tool_input", {}).get("questions", []) or []
    first = questions[0] if isinstance(questions, list) and questions else {}
    if not isinstance(first, dict):
        first = {}
    question_text = str(first.get("question", "")).strip()
    if not question_text:
        # No question text — fail open rather than emit a deny that
        # blocks an empty payload the user can debug separately.
        return False
    options = first.get("options", []) if isinstance(first.get("options"), list) else []
    queue_id = _record_deferred_question(question_text, options, data)
    if queue_id is None:
        # Teatree unavailable — fail open so the user is never blocked
        # by a hook crash. The standard interactive flow then runs.
        return False
    reason = (
        f"availability=away — your question was captured durably as DeferredQuestion #{queue_id} "
        f"and the user will answer it via `t3 questions answer {queue_id} <text>`. "
        "Proceed with any work that does not depend on the answer; the response will surface "
        "in a future turn's additionalContext when the user resolves it."
    )
    return emit_pretooluse_deny(reason)


# ── UserPromptSubmit: inject pending-question backlog into context ────────────


def handle_inject_pending_questions(_data: dict) -> None:
    """Append the pending-question backlog to ``additionalContext``.

    Lets the agent see, on every user turn, which deferred questions
    are still waiting on a user answer — so it can prioritise work
    that does NOT depend on those answers and avoid asking the same
    question again. Fails open: if teatree is unavailable, just skip.
    """
    if not _bootstrap_teatree_django():
        return
    try:
        from teatree.core.availability import pending_questions_count  # noqa: PLC0415
        from teatree.core.models.deferred_question import DeferredQuestion  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return
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
    if not _bootstrap_teatree_django():
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
    if not _bootstrap_teatree_django():
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


def handle_bare_reference_stop(data: dict) -> bool | None:
    """Warn when the assistant's final chat text cites a bare reference (#1530).

    The Stop-time soft sibling of the #1530 PreToolUse hard gate. The
    shown chat message cannot be retracted, so this never denies — it
    emits a top-level ``systemMessage`` WARNING that surfaces the
    violation next turn and gives a recurrence signal. Low-noise:
    matches only clear bare ``#NNNN`` / ``!NNNN`` tokens not already
    wrapped in a clickable link, reusing the shared detector.

    Fail-safe-to-silent: any malformed input or missing transcript
    returns ``None`` so the Stop chain is never crashed.
    """
    src_dir = Path(__file__).resolve().parents[2] / "src"
    added = False
    try:
        if str(src_dir) not in sys.path:
            sys.path.insert(0, str(src_dir))
            added = True
        return _run_bare_reference_stop(data)
    except Exception:  # noqa: BLE001 — Stop hook must be crash-proof
        return None
    finally:
        if added:
            with contextlib.suppress(ValueError):
                sys.path.remove(str(src_dir))


def _run_bare_reference_stop(data: dict) -> bool | None:
    from teatree.hooks import bare_reference_scanner  # noqa: PLC0415

    turn = _last_assistant_turn(data.get("transcript_path", ""))
    if turn is None:
        return None
    refs = bare_reference_scanner.find_bare_references(turn[0])
    if not refs:
        return None
    json.dump({"systemMessage": bare_reference_scanner.format_warn_message(refs)}, sys.stdout)
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


def handle_subagent_stop_no_commit(data: dict) -> None:
    """SubagentStop: record a work-branch worktree that produced 0 commits (#1205).

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

        finding = no_commit_detector.detect(worktree)
        if finding.is_flagged:
            _record_no_commit_signal(data.get("session_id", ""), finding)
    except Exception as exc:  # noqa: BLE001 — SubagentStop hook must be crash-proof
        print(  # noqa: T201 — hook stderr is the module's logging channel
            f"[hook_router] no-commit detection skipped (unexpected error: {exc})",
            file=sys.stderr,
        )


_HANDLERS: dict[str, list] = {
    "UserPromptSubmit": [
        handle_clear_classifier_deny_marker,
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
        handle_enforce_plan_gate,
        handle_enforce_agent_plan_gate,
        handle_protect_default_branch,
        handle_bare_reference_pretool,
        handle_quote_scanner_pretool,
        handle_dispatch_prompt_quote_scanner,
        handle_banned_terms_pretool,
        handle_enforce_skill_loading,
        handle_block_direct_commands,
        handle_block_out_of_band_merge,
        handle_validate_mr_metadata,
        handle_block_ai_signature,
        handle_block_uncovered_diff,
        handle_enforce_orchestrator_boundary,
        handle_mirror_question_to_slack,
    ],
    "PostToolUse": [
        handle_track_classifier_denial,
        handle_track_active_repo,
        handle_track_skill_usage,
        handle_track_cron_jobs,
        handle_read_dedup,
        handle_track_todos,
        handle_track_agents,
        handle_track_plan_invocation,
        handle_track_plan_skill_timestamp,
        handle_track_workspace_source_read,
    ],
    "TaskCreated": [handle_enforce_skill_loading_on_task_create],
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
        handle_enforce_answered_questions,
        handle_bare_reference_stop,
        handle_consideration_gate,
        handle_loop_self_pump,
    ],
    "SubagentStop": [handle_subagent_stop_no_commit],
}


def main() -> None:
    args = _parse_args()
    handlers = _HANDLERS.get(args.event, [])
    if not handlers:
        return

    data = _read_input()
    if not data:
        return

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

    # Claude Code 2.1.146 changelog: PreToolUse hooks that emit deny JSON
    # are only honoured when the process exits with code 2. An exit-0 deny
    # is silently dropped and falls through to the auto-mode classifier.
    # See #1447.
    if deny_emitted:
        sys.exit(2)


if __name__ == "__main__":
    main()
