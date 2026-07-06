"""SDK option-building for the headless runner — the real-environment options.

Split out of :mod:`teatree.agents.headless` for the module-health LOC cap: the
``ClaudeAgentOptions`` builder plus its model-tiering glue (:func:`_build_options`),
the worktree-cwd resolver (:func:`_resolve_task_cwd`), the resumable-session walker
(:func:`_get_resume_session_id`), and the spawn constants they read. Re-exported
from ``teatree.agents.headless`` so ``from teatree.agents.headless import
_build_options`` (and the ``_MAX_TURNS`` / ``_PERMISSION_MODE`` / ``UUID_RE`` /
``_resolve_task_cwd`` / ``_get_resume_session_id`` sites in ``teams.pane_spawn`` and
``core.management.commands.tasks``) stays valid.
"""

import re
from pathlib import Path
from typing import cast

from claude_agent_sdk import ClaudeAgentOptions
from claude_agent_sdk.types import EffortLevel, SystemPromptPreset, ThinkingConfig

from teatree.agents.model_tiering import model_supports_thinking, resolve_spawn_effort, resolve_spawn_model
from teatree.agents.reader_profile import is_reader_phase
from teatree.agents.sdk_tool_map import sdk_disallowed_tools_for_phase
from teatree.core.models import Task
from teatree.core.models.worktree import Worktree

# Headless agent default permission mode: a detached run has no human to grant
# tool permissions, so it bypasses the per-tool prompt and runs unattended.
_PERMISSION_MODE = "bypassPermissions"
# The SDK spawns no max-turns ceiling of its own; the loop watchdog bounds a
# runaway. ``0`` leaves the SDK uncapped (the watchdog is the real bound).
_MAX_TURNS = 0
# AskUserQuestion only renders to a live human at the harness — there is none
# in the SDK/headless lane, so leaving it allowed lets the agent silently stall
# on an unanswerable question. Hard-deny it UNCONDITIONALLY: the agent must
# instead return the structured ``needs_user_input`` + ``user_input_reason`` and
# STOP, which the durable DeferredQuestion → Slack → resume loop then routes to
# the user. The per-phase least-privilege complement (PR-11) is added on top of
# this floor at build time — see :func:`_disallowed_tools_for_phase`.
_DISALLOWED_TOOLS = ("AskUserQuestion",)
# #116 reader hardening: built-in tools OUTSIDE the ``phase_tools`` capability
# vocabulary (so not covered by ``sdk_disallowed_tools_for_phase``) that a
# bypassPermissions spawn could otherwise reach. Denied by name for the reader phase
# on top of the full capability complement, so the reader has NO built-in either.
_READER_EXTRA_DENIED_TOOLS = ("SlashCommand", "TodoWrite", "ExitPlanMode")
# Adaptive thinking, pinned EXPLICITLY on every reasoning-capable production
# spawn. Opus 4.8 runs WITHOUT thinking when the ``thinking`` option is omitted,
# so the Opus-4.8 planning/coding/debugging/reviewing phases would silently lose
# extended thinking; setting adaptive makes them deterministically think (the
# model still decides HOW MUCH). GUARDED by
# :func:`~teatree.agents.model_tiering.model_supports_thinking` so the cheap/Haiku
# tier — which rejects the lever — never receives it.
_ADAPTIVE_THINKING: ThinkingConfig = {"type": "adaptive"}

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _disallowed_tools_for_phase(phase: str) -> list[str]:
    """The full disallow list for a headless dispatch — floor plus per-phase complement.

    :data:`_DISALLOWED_TOOLS` (``AskUserQuestion``) is denied on every headless
    spawn; the per-phase least-privilege complement (PR-11) is layered on top,
    mapped from the phase_tools SSOT to SDK tool names by
    :func:`~teatree.agents.sdk_tool_map.sdk_disallowed_tools_for_phase`. A review
    phase (``reviewing`` / ``e2e_reviewing`` / ``requesting_review``) therefore
    denies the shell (git-write), ``Write``/``Edit``, and the spawn tools — the
    cold-review least-privilege that keeps the transcript at its verdict. A write
    phase's complement is empty, so its list stays exactly ``[AskUserQuestion]``,
    byte-identical to before the lever. The #116 reader phase additionally denies the
    non-capability built-ins (:data:`_READER_EXTRA_DENIED_TOOLS`) so no tool of ANY
    kind remains. Sorted & deduplicated for determinism.
    """
    denied = set(_DISALLOWED_TOOLS) | set(sdk_disallowed_tools_for_phase(phase))
    if is_reader_phase(phase):
        denied |= set(_READER_EXTRA_DENIED_TOOLS)
    return sorted(denied)


def _build_options(
    task: Task,
    system_context: str,
    *,
    phase: str,
    skills: list[str],
    env: dict[str, str] | None = None,
) -> ClaudeAgentOptions:
    """Build the REAL-environment SDK options for a headless task.

    Mirrors what the deleted ``_build_headless_command`` passed: the appended
    system context, the resolved spawn model (the most-capable-wins floor merge
    of the per-phase tier and the per-skill MODEL floors of the loaded skills,
    else the user's default), the per-tier reasoning effort for the same phase
    (:func:`resolve_spawn_effort` — ``xhigh`` for a frontier phase, ``high`` for a
    balanced phase, unset for the cheap/Haiku phases), the worktree as ``cwd`` /
    ``add_dirs``, and the parent session to resume. NO clean-room isolation — a
    headless run executes a real task and needs the real environment, skills, and
    project context.

    ``env`` (when supplied by :func:`_provider_child_env`) pins the credential for
    the chosen ``agent_harness_provider`` on the spawned ``claude`` child; ``None``
    leaves the SDK default (inherit the ambient env), byte-identical to before.
    """
    cwd = _resolve_task_cwd(task)
    add_dirs = [cwd] if cwd else []
    resume_session_id = _get_resume_session_id(task)
    # session_id + task pk are threaded so a situational honesty-critical
    # escalation (teatree#2263) can raise a verification spawn to the most-honest
    # model; both default absent → byte-identical to today when none is active.
    escalation_session_id = resume_session_id or (task.session.agent_id if task.session_id else "")  # ty: ignore[unresolved-attribute]
    spawn_model = resolve_spawn_model(
        phase,
        skills=skills,
        session_id=escalation_session_id or None,
        task_id=int(task.pk),
    )
    options = ClaudeAgentOptions(
        # APPEND to the claude_code preset, never REPLACE it: a plain-str
        # system_prompt maps to --system-prompt (the deleted ``claude -p`` path
        # used --append-system-prompt), which would drop the Claude Code preset
        # on every production headless run.
        system_prompt=SystemPromptPreset(type="preset", preset="claude_code", append=system_context),
        model=spawn_model or None,
        cwd=cwd,
        add_dirs=add_dirs,
        permission_mode=_PERMISSION_MODE,
        disallowed_tools=_disallowed_tools_for_phase(phase),
        max_turns=_MAX_TURNS,
        resume=resume_session_id or None,
        # Pin adaptive thinking so the Opus-4.8 reasoning phases think (Opus 4.8
        # omits thinking by default). Guarded so the cheap/Haiku tier — which
        # rejects the lever — and an inherited-default spawn (``None``) keep the
        # SDK default.
        thinking=_ADAPTIVE_THINKING if model_supports_thinking(spawn_model) else None,
        # Pin the per-abstract-TIER reasoning effort for the SAME phase the model
        # resolved from (frontier → xhigh, balanced → high). ``None`` for the
        # cheap/Haiku phases (which reject the lever) and a sentinel-opted-out
        # phase, so those spawns inherit the SDK default effort. The resolver
        # returns the domain ``str | None`` (validated to the effort scale);
        # cast it to the SDK ``EffortLevel`` literal at this boundary.
        effort=cast("EffortLevel | None", resolve_spawn_effort(phase)),
    )
    if env is not None:
        options.env = env
    if is_reader_phase(phase):
        _apply_reader_tool_lockdown(options)
    return options


def _apply_reader_tool_lockdown(options: ClaudeAgentOptions) -> None:
    """Close the #116 reader's tool-acquisition residual: load NO settings, NO MCP config.

    The ``disallowed_tools`` denylist covers every capability tool + every named built-in
    (:func:`_disallowed_tools_for_phase`), but under ``bypassPermissions`` a tool the
    denylist does not name — an MCP-server tool, a custom slash command loaded from
    ``~/.claude`` / project settings — would still be reachable. Loading NO setting
    sources (``--setting-sources=`` empty) and NO MCP config (``strict_mcp_config`` +
    empty ``mcp_servers``) removes every such source, so the reader has zero tools from
    any origin. An empty ``allowed_tools`` is NOT the mechanism — the SDK omits the
    ``--allowedTools`` flag when the list is empty, so it would be a silent no-op; the
    closure is source-suppression, verified against the SDK transport.
    """
    options.setting_sources = []
    options.mcp_servers = {}
    options.strict_mcp_config = True


def _resolve_task_cwd(task: Task) -> str | None:
    """Determine the working directory for a task from its ticket's worktrees."""
    worktree = Worktree.objects.filter(ticket=task.ticket).order_by("pk").first()
    if worktree and Path(worktree.repo_path).is_dir():
        return str(worktree.repo_path)
    return None


def _get_resume_session_id(task: Task) -> str:
    """Walk the parent_task chain to find a resumable Claude session.

    When a headless task follows an interactive one (or vice versa),
    the session_id from the previous run lets us resume with full context.
    """
    current = task.parent_task
    while current is not None:
        last_attempt = current.attempts.order_by("-pk").first()
        if last_attempt and last_attempt.agent_session_id and UUID_RE.match(last_attempt.agent_session_id):
            return last_attempt.agent_session_id
        agent_id = current.session.agent_id if current.session_id else ""
        if agent_id and UUID_RE.match(agent_id):
            return agent_id
        current = current.parent_task
    return ""
