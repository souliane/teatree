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

from teatree.agents import permission_modes
from teatree.agents.model_tiering import model_supports_thinking, resolve_spawn_effort, resolve_spawn_model
from teatree.agents.reader_profile import is_reader_phase
from teatree.agents.sdk_tool_map import sdk_disallowed_tools_for_phase
from teatree.core.models import Task
from teatree.core.models.worktree import Worktree
from teatree.llm.builtin_tools import KNOWN_BUILTIN_TOOLS

_PERMISSION_MODE = permission_modes.UNATTENDED
_READER_PERMISSION_MODE = permission_modes.READER_DEFAULT_DENY
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
    byte-identical to before the lever. The #116 reader phase denies the EXHAUSTIVE
    :data:`~teatree.llm.builtin_tools.KNOWN_BUILTIN_TOOLS` set (the binary-validated
    registry — its available set is empty), so every known built-in including the
    external-effect ones (``PushNotification`` / ``RemoteTrigger``) and ``ToolSearch``
    is denied — no tool of ANY kind remains. Sorted & deduplicated for determinism.
    """
    denied = set(_DISALLOWED_TOOLS) | set(sdk_disallowed_tools_for_phase(phase))
    if is_reader_phase(phase):
        denied |= set(KNOWN_BUILTIN_TOOLS)
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
        #
        # ``exclude_dynamic_sections`` strips the preset's per-run content (cwd,
        # git status, auto-memory) out of the system prefix and re-injects it into
        # the first user message. Prompt caching on this lane is CLI-internal and
        # exposes no ``cache_control`` surface, so prefix STABILITY is the only
        # lever teatree has over the hit rate: without this, git status churning
        # between dispatches in the same worktree invalidates the cached prefix
        # every time. Older CLIs ignore the option silently.
        system_prompt=SystemPromptPreset(
            type="preset",
            preset="claude_code",
            append=system_context,
            exclude_dynamic_sections=True,
        ),
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
    else:
        _wire_teatree_mcp_server(options)
    return options


def _wire_teatree_mcp_server(options: ClaudeAgentOptions) -> None:
    """Inject teatree's own local-stdio MCP server so lifecycle sub-agents reach it (#3242).

    Claude Code does not forward a local-stdio server (``t3 mcp serve``) to a
    sub-agent, and it ignores the ``mcpServers`` frontmatter on a plugin-provided
    sub-agent definition — so the shipped ``.mcp.json`` alone never gives the
    dispatched coder/reviewer/shipper the ``mcp__teatree__*`` structured-read
    tools; they fall back to shelling out to the ``t3`` CLI. The headless
    dispatch owns its options, so it wires the server explicitly here. The
    launch command mirrors ``.mcp.json`` (:mod:`teatree.core.mcp_registration`
    is the single source of truth). Skipped for the #116 reader, which stays
    hermetic (:func:`_apply_reader_tool_lockdown`).
    """
    from teatree.core.mcp_registration import (  # noqa: PLC0415 — deferred: keeps the option-build import light
        EXPECTED_ARGS,
        EXPECTED_COMMAND,
        TEATREE_MCP_SERVER_NAME,
    )

    existing = options.mcp_servers if isinstance(options.mcp_servers, dict) else {}
    options.mcp_servers = {
        **existing,
        TEATREE_MCP_SERVER_NAME: {"type": "stdio", "command": EXPECTED_COMMAND, "args": list(EXPECTED_ARGS)},
    }


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

    :data:`_READER_PERMISSION_MODE` closes the same residual from the other side.
    ``bypassPermissions`` auto-approves whatever survives; ``dontAsk`` denies anything
    not pre-approved by an allow rule, and the reader carries none — so an unnamed tool
    reaching the reader by any route is refused by DEFAULT rather than by enumeration.
    Source-suppression and default-deny are independent, and the reader keeps both.
    """
    options.setting_sources = []
    options.mcp_servers = {}
    options.strict_mcp_config = True
    options.permission_mode = _READER_PERMISSION_MODE


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
