"""Two-stage cheap answer builder for ``SIMPLE`` Slack questions (#1014).

Stage A (0 tokens): return teatree's already-rendered statusline content
(the same lines the user sees on disk under :func:`statusline.default_path`),
transformed for Slack mrkdwn. No LLM, no dashboard table — the user
expects to see only the entries that appear in the statusline (#1121).

Stage B (only if A yields nothing): exactly ONE ``claude -p --model
haiku`` call with a tiny ``--append-system-prompt`` and a <1 KB compact
state digest (the same statusline content), NO skills / tools / loop
context. It is hard-gated by ``T3_SLACK_ANSWER_TOKEN_BUDGET`` (reusing the
self-improve :func:`precheck_budget`). If the model decides it must read
code / investigate, it replies with the exact :data:`NEEDS_WORK_SENTINEL`
token and the caller delegates to a sub-agent instead.

Returning ``None`` means "could not cheaply answer, and did not even try
the model" (budget gate closed) — the cycle then falls through to the
NEEDS_WORK delegation path.
"""

import os
from typing import TYPE_CHECKING

from teatree.loop.self_improve.budget import precheck_budget
from teatree.loop.slack_answer.classifier import strip_urls
from teatree.loop.statusline import statusline_for_slack
from teatree.utils.run import CommandFailedError, run_checked

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


def _run_haiku(question: str, digest: str) -> str:
    """One bounded ``claude -p --model haiku`` call; returns its text.

    No skills, no tools, no loop context — a single stateless turn. A
    non-zero exit or timeout yields the NEEDS_WORK sentinel so the caller
    falls through to delegation rather than posting a broken answer.
    """
    import shutil  # noqa: PLC0415

    binary = shutil.which("claude")
    if binary is None:
        return NEEDS_WORK_SENTINEL
    prompt = f"Question: {question}\n\nCompact teatree state:\n{digest}"
    cmd = [
        binary,
        "--model",
        "haiku",
        "--append-system-prompt",
        _HAIKU_SYSTEM_PROMPT,
        "-p",
        prompt,
    ]
    try:
        result = run_checked(cmd, timeout=_HAIKU_TIMEOUT_SECONDS)
    except (CommandFailedError, OSError, TimeoutError):
        return NEEDS_WORK_SENTINEL
    return result.stdout.strip() or NEEDS_WORK_SENTINEL


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
