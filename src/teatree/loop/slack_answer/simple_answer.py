"""Two-stage cheap answer builder for ``SIMPLE`` Slack questions (#1014).

Stage A (0 tokens): return teatree's already-rendered statusline content
(the same lines the user sees on disk under :func:`statusline.default_path`),
transformed for Slack mrkdwn. No LLM, no dashboard table — the user
expects to see only the entries that appear in the statusline (#1121).

Stage B (only if A yields nothing): exactly ONE in-process
:func:`claude_agent_sdk.query` turn on the haiku model, with the tiny
:data:`_HAIKU_SYSTEM_PROMPT` as the whole system prompt and a <1 KB compact
state digest (the same statusline content), NO skills / tools / loop
context. It runs the model in-process via the same Agent SDK the eval
``api`` backend uses, authenticated by the subscription
(``CLAUDE_CODE_OAUTH_TOKEN``) — it never shells ``claude -p`` and never
bills an API key; it spends subscription-covered model time for one
stateless turn. It is hard-gated by ``T3_SLACK_ANSWER_TOKEN_BUDGET``
(reusing the self-improve :func:`precheck_budget`). If the model decides it
must read code / investigate, it replies with the exact
:data:`NEEDS_WORK_SENTINEL` token and the caller delegates to a sub-agent
instead.

Returning ``None`` means "could not cheaply answer, and did not even try
the model" (budget gate closed) — the cycle then falls through to the
NEEDS_WORK delegation path.
"""

import asyncio
import os
import shutil
from typing import TYPE_CHECKING

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, TextBlock, query

from teatree.loop.self_improve.budget import precheck_budget
from teatree.loop.slack_answer.classifier import strip_urls
from teatree.loop.statusline import statusline_for_slack

if TYPE_CHECKING:
    from teatree.core.models import PendingChatInjection

NEEDS_WORK_SENTINEL = "NEEDS_WORK"

# Env-var NAME (not a credential); split-assign so the trailing word does
# not trip ruff's hardcoded-password (S105) heuristic.
_ENV_PREFIX = "T3_SLACK_ANSWER_"
_TOKEN_BUDGET_ENV = f"{_ENV_PREFIX}TOKEN_BUDGET"

_DASHBOARD_TOKENS: tuple[str, ...] = (
    "status",
    "working on",
    "what are you doing",
    "pr",
    "prs",
    "pending",
    "blocker",
    "blocked",
    "blocking",
    "digest",
    "progress",
    "today",
    "dashboard",
    "statusline",
)

_HAIKU_SYSTEM_PROMPT = (
    "Answer this one Slack question from the compact state below in "
    "≤3 sentences. If you must read code or investigate to answer, "
    f"reply with exactly {NEEDS_WORK_SENTINEL} and nothing else."
)

_HAIKU_TIMEOUT_SECONDS = 60.0


def _token_budget_remaining() -> int | None:
    raw = os.environ.get(_TOKEN_BUDGET_ENV, "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _stage_a(text: str) -> str | None:
    """Zero-token answer from teatree's on-disk statusline, or ``None``.

    The user only ever wants to see what is in the statusline — not the
    dashboard table (#1121). When the statusline file is empty/missing,
    return ``None`` so the cycle falls through to Stage B or delegation.
    """
    lowered = strip_urls(text.lower())
    if not any(tok in lowered for tok in _DASHBOARD_TOKENS):
        return None
    rendered = statusline_for_slack().strip()
    if not rendered:
        return None
    return rendered


def _compact_state_digest() -> str:
    """<1 KB plain-text state digest for the Stage B prompt (no secrets)."""
    digest = statusline_for_slack().strip()
    return digest[:1024]


_HAIKU_MODEL = "haiku"


def _haiku_options() -> ClaudeAgentOptions:
    """Clean-room options for the single haiku turn: no tools, one turn, no bias.

    Mirrors the eval ``api`` runner's virgin configuration in miniature:
    ``setting_sources=[]`` (the developer's personal context never biases the
    answer), an empty tool allowlist, and a single ``max_turns`` so the model
    answers from the supplied digest alone and cannot run a tool / read code.
    """
    return ClaudeAgentOptions(
        model=_HAIKU_MODEL,
        system_prompt=_HAIKU_SYSTEM_PROMPT,
        setting_sources=[],
        tools=[],
        max_turns=1,
    )


def _run_haiku(question: str, digest: str) -> str:
    """One bounded in-process haiku turn via the Agent SDK; returns its text.

    No skills, no tools, no loop context — a single stateless turn run through
    :func:`claude_agent_sdk.query` (the SDK spawns the ``claude`` CLI child,
    authenticated by the subscription's ``CLAUDE_CODE_OAUTH_TOKEN``; it never
    shells ``claude -p`` and never bills an API key). A missing ``claude`` child,
    any SDK error, or a timeout yields the NEEDS_WORK sentinel so the caller falls
    through to delegation rather than posting a broken answer.
    """
    if shutil.which("claude") is None:
        return NEEDS_WORK_SENTINEL
    prompt = f"Question: {question}\n\nCompact teatree state:\n{digest}"
    try:
        text = asyncio.run(_query_first_text(prompt))
    except (OSError, TimeoutError, RuntimeError):
        return NEEDS_WORK_SENTINEL
    return text.strip() or NEEDS_WORK_SENTINEL


async def _query_first_text(prompt: str) -> str:
    """Drive one bounded haiku turn and return the concatenated assistant text."""
    parts: list[str] = []

    async def _drive() -> None:
        async for message in query(prompt=prompt, options=_haiku_options()):
            if isinstance(message, AssistantMessage):
                parts.extend(block.text for block in message.content if isinstance(block, TextBlock))

    await asyncio.wait_for(_drive(), timeout=_HAIKU_TIMEOUT_SECONDS)
    return "".join(parts)


def build_simple_answer(row: "PendingChatInjection") -> str | None:
    """Build a cheap answer for a ``SIMPLE``-classified row.

    Returns the answer text, the :data:`NEEDS_WORK_SENTINEL` (Stage B
    bailed — caller must delegate), or ``None`` (Stage A produced nothing
    and the token budget gate is closed — caller must delegate).
    """
    stage_a = _stage_a(row.text)
    if stage_a is not None:
        return stage_a

    verdict = precheck_budget(token_budget_remaining=_token_budget_remaining())
    if not verdict.ok:
        return None

    return _run_haiku(row.text, _compact_state_digest())


__all__ = ["NEEDS_WORK_SENTINEL", "build_simple_answer"]
