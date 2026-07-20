"""Two-stage cheap answer builder for ``SIMPLE`` Slack questions (#1014).

Stage A (0 tokens): return teatree's already-rendered statusline content
(the same lines the user sees on disk under :func:`statusline.default_path`),
transformed for Slack mrkdwn. No LLM, no dashboard table — the user
expects to see only the entries that appear in the statusline (#1121).

Stage B (only if A yields nothing): exactly ONE clean-room, cheap-tier turn
through the shared one-shot seam
(:func:`teatree.agents.one_shot.run_one_shot`) with the tiny
:data:`_CHEAP_SYSTEM_PROMPT` as the whole system prompt and a <1 KB compact
state digest (the same statusline content), NO skills / tools / loop
context. The seam resolves the ``cheap`` tier to a concrete model id and
routes the turn through the active harness (``claude_sdk`` or
``pydantic_ai``/OrcaRouter), so the answer follows a swapped tier-model DB
row and works off-Claude — never a hardcoded model id. It is hard-gated by
``T3_SLACK_ANSWER_TOKEN_BUDGET`` (reusing the self-improve
:func:`precheck_budget`). If the model decides it must read code /
investigate, it replies with the exact :data:`NEEDS_WORK_SENTINEL` token and
the caller delegates to a sub-agent instead.

Returning ``None`` means "could not cheaply answer, and did not even try
the model" (budget gate closed) — the cycle then falls through to the
NEEDS_WORK delegation path.
"""

import os
from typing import TYPE_CHECKING

from teatree.agents.one_shot import OneShotSpec, run_one_shot
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

_CHEAP_SYSTEM_PROMPT = (
    "Answer this one Slack question from the compact state below in "
    "≤3 sentences. If you must read code or investigate to answer, "
    f"reply with exactly {NEEDS_WORK_SENTINEL} and nothing else."
)

_CHEAP_TURN_TIMEOUT_SECONDS = 60.0


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


def _run_cheap_turn(question: str, digest: str) -> str:
    """One bounded, clean-room, cheap-tier turn via the shared one-shot seam; returns its text.

    No skills, no tools, no loop context — a single stateless turn resolved to
    the ``cheap`` tier and routed through the active harness
    (:func:`teatree.agents.one_shot.run_one_shot`). A missing ``claude`` child,
    a backend error, or a timeout yields ``None`` from the seam, which collapses
    to the NEEDS_WORK sentinel so the caller falls through to delegation rather
    than posting a broken answer.

    A refused ambient environment does NOT collapse that way — the seam raises
    :class:`~teatree.llm.credentials.CredentialError` and it propagates through
    this function, failing the cycle loudly. Answering NEEDS_WORK on a misrouted
    base URL would delegate every question forever with nothing naming why.
    """
    prompt = f"Question: {question}\n\nCompact teatree state:\n{digest}"
    answer = run_one_shot(
        prompt,
        OneShotSpec(
            system_prompt=_CHEAP_SYSTEM_PROMPT,
            tier="cheap",
            max_turns=1,
            timeout_seconds=_CHEAP_TURN_TIMEOUT_SECONDS,
        ),
    )
    return answer or NEEDS_WORK_SENTINEL


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

    return _run_cheap_turn(row.text, _compact_state_digest())


__all__ = ["NEEDS_WORK_SENTINEL", "build_simple_answer"]
