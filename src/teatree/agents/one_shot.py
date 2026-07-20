"""One shared ``run_one_shot`` seam for cheap, single-turn aux LLM calls.

The aux one-shot call sites — Slack ``simple_answer``, ``ticket_short_describe``
— used to hardcode ``claude-haiku-4-5`` and drive :func:`claude_agent_sdk.query`
directly, bypassing BOTH the tier-resolution seam and the harness seam, so they
silently broke off-Claude (§3a #1, §7 #2). This module collapses them onto ONE
helper that:

*   resolves the abstract TIER to a concrete model id
    (:func:`~teatree.agents.model_tiering.resolve_tier`), so a swapped tier-model
    DB row reaches the call; and
*   routes the turn through the provider-agnostic harness seam
    (:func:`~teatree.agents.harness.resolve_harness`), so the SAME clean-room
    turn runs on ``claude_sdk`` or ``pydantic_ai``/OrcaRouter with no code edit.

The turn is CLEAN-ROOM: an empty ``setting_sources`` and empty ``settings`` (no
hooks), no tools, and a single ``max_turns`` so the model answers from the
supplied prompt alone — the developer's personal context never biases the
result, and the model cannot run a tool or read code. Any failure (a missing
``claude`` binary, a credential problem, a timeout, an SDK/provider error)
degrades to ``None`` so a best-effort aux turn never breaks its caller.
"""

import asyncio
from dataclasses import dataclass

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, TextBlock

from teatree.agents.harness import Harness, resolve_harness
from teatree.agents.model_tiering import resolve_tier
from teatree.llm.credentials import reject_ambient_base_url_redirect

# The empty-hooks ``settings`` blob that, with ``setting_sources=[]``, keeps the
# clean-room turn from picking up the developer's hooks/personal context.
_EMPTY_HOOKS = '{"hooks":{}}'


@dataclass(frozen=True, slots=True)
class OneShotSpec:
    """The knobs for one clean-room aux turn.

    *   ``system_prompt`` — the WHOLE system prompt for the turn (not appended to
        any preset). A tiny task instruction, not a skill bundle.
    *   ``tier`` — the abstract tier resolved to a concrete model id
        (:func:`~teatree.agents.model_tiering.resolve_tier`). Defaults to
        ``cheap`` — these are mechanical, sub-1-KB turns.
    *   ``max_turns`` — the hard turn cap (1 by default: one stateless answer).
    *   ``timeout_seconds`` — the wall-clock watchdog; the turn is abandoned
        (``None`` result) if it overruns.
    """

    system_prompt: str
    tier: str = "cheap"
    max_turns: int = 1
    timeout_seconds: float = 60.0


def _clean_room_options(spec: OneShotSpec) -> ClaudeAgentOptions:
    """The clean-room :class:`ClaudeAgentOptions` for *spec* — tier-resolved, no tools.

    ``model`` is the tier resolved to a concrete id, so an ``agent_tier_models``
    DB row reaches the turn. ``setting_sources=[]`` + ``settings`` (empty hooks) +
    ``strict_mcp_config`` keep the run virgin; an empty ``tools`` allowlist and a
    single ``max_turns`` bound it to one context-free answer.

    This turn pins no credential, so the spawned child inherits the ambient auth
    state AND an ambient ``ANTHROPIC_BASE_URL``. The redirect guard runs HERE rather
    than inside :func:`run_one_shot`'s try, which swallows every exception: a
    misconfiguration that silently routed a plan-authenticated turn to a third-party
    endpoint is the one failure this helper must NOT degrade quietly into ``None``.
    """
    reject_ambient_base_url_redirect()
    return ClaudeAgentOptions(
        model=resolve_tier(spec.tier),
        system_prompt=spec.system_prompt,
        setting_sources=[],
        settings=_EMPTY_HOOKS,
        strict_mcp_config=True,
        tools=[],
        max_turns=spec.max_turns,
    )


async def _run_turn(harness: Harness, options: ClaudeAgentOptions, prompt: str, *, timeout_seconds: float) -> str:
    """Drive ONE turn through the harness seam and return the concatenated assistant text."""

    async def _collect() -> str:
        parts: list[str] = []
        async with harness.open(options) as session:
            await session.query(prompt)
            async for message in session.receive_response():
                if isinstance(message, AssistantMessage):
                    parts.extend(block.text for block in message.content if isinstance(block, TextBlock))
        return "".join(parts)

    return await asyncio.wait_for(_collect(), timeout=timeout_seconds)


def run_one_shot(prompt: str, spec: OneShotSpec, *, harness: Harness | None = None) -> str | None:
    """Run one clean-room, tier-resolved, harness-routed turn for *prompt*; ``None`` on failure.

    Resolves the harness (:func:`~teatree.agents.harness.resolve_harness`, or the
    injected *harness* for tests) and drives a single clean-room turn built from
    *spec*. Returns the stripped assistant text, or ``None`` when the turn
    produced nothing OR any failure occurred (a missing ``claude`` binary, a
    credential problem, a timeout, an SDK/provider error) — a best-effort aux
    turn must degrade quietly, never break its caller.
    """
    options = _clean_room_options(spec)
    resolved = harness if harness is not None else resolve_harness()
    try:
        text = asyncio.run(_run_turn(resolved, options, prompt, timeout_seconds=spec.timeout_seconds))
    except Exception:  # noqa: BLE001 — the documented contract is "None on ANY failure"; heterogeneous backend errors
        return None
    return text.strip() or None


__all__ = ["OneShotSpec", "run_one_shot"]
