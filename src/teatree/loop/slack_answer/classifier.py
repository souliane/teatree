"""Zero-token routing for inbound Slack messages (#1014).

:func:`classify` is pure Python — no DB, no network, no LLM. It decides
which of three cheap paths a user's Slack DM takes:

- ``ACK_ONLY`` — a thanks / ok / 👍 that needs only a reaction, no reply.
- ``SIMPLE`` — a question answerable from teatree's own DB state
    (status / what-working-on / which-PRs / pending / blockers / digest).
- ``NEEDS_WORK`` — an imperative (fix/implement/…) or an
    investigation-needing question that must be delegated to a sub-agent.

The contract is **fail-safe to ``NEEDS_WORK``**: anything the cheap
heuristics cannot confidently place as ack or DB-answerable is delegated,
never silently swallowed. A missed ack costs one wasted reaction; a
missed real request would drop the user's ask on the floor.
"""

import re
from enum import StrEnum

# Length ceiling for an ack: longer messages carry content even if they
# open with "thanks".
_ACK_MAX_CHARS = 40

_ACK_TOKENS: frozenset[str] = frozenset(
    {
        "thanks",
        "thank you",
        "thx",
        "ty",
        "ok",
        "okay",
        "k",
        "kk",
        "got it",
        "gotit",
        "lgtm",
        "great",
        "perfect",
        "cool",
        "nice",
        "awesome",
        "sounds good",
        "sg",
        "done",
        "ack",
        "👍",
        "🙏",
        "🎉",
        "✅",
        "💯",
    }
)

# Imperative verbs that mean "go do work" — presence routes to NEEDS_WORK
# regardless of question marks.
_IMPERATIVE_VERBS: frozenset[str] = frozenset(
    {
        "fix",
        "implement",
        "investigate",
        "change",
        "add",
        "remove",
        "delete",
        "refactor",
        "debug",
        "build",
        "create",
        "update",
        "write",
        "run",
        "deploy",
        "merge",
        "rebase",
        "revert",
        "look into",
        "check why",
        "find out why",
        "figure out",
    }
)

# Tokens that signal a DB-answerable status question.
_SIMPLE_QUESTION_TOKENS: tuple[str, ...] = (
    "status",
    "working on",
    "what are you doing",
    "which pr",
    "which prs",
    "what prs",
    "open pr",
    "open prs",
    "pending",
    "blocker",
    "blocked",
    "blocking",
    "today",
    "digest",
    "progress",
)

# A "why … fail/break/error" question needs investigation, not a DB read.
_INVESTIGATION_RE = re.compile(
    r"\bwhy\b.*\b(fail|failed|failing|break|broke|broken|error|red|wrong|crash)",
    re.IGNORECASE,
)

_EMOJI_ONLY_RE = re.compile(r"^[\W_]+$", re.UNICODE)

_URL_RE = re.compile(r"<?https?://\S+")


class AnswerRoute(StrEnum):
    """The three cheap paths a classified Slack message can take."""

    ACK_ONLY = "ack_only"
    SIMPLE = "simple"
    NEEDS_WORK = "needs_work"


def strip_urls(text: str) -> str:
    return _URL_RE.sub(" ", text)


def _normalized(text: str) -> str:
    return text.strip().lower()


def _has_imperative(lowered: str) -> bool:
    for verb in _IMPERATIVE_VERBS:
        if " " in verb:
            if verb in lowered:
                return True
        elif re.search(rf"\b{re.escape(verb)}\b", lowered):
            return True
    return False


def _is_ack(text: str, lowered: str) -> bool:
    """True iff *text* is a short acknowledgement.

    ``classify`` already excluded imperatives and investigations before
    calling this, so no redundant guard for them here — the only
    disqualifiers left are a question mark or a content-bearing length.
    """
    if "?" in text or len(text.strip()) > _ACK_MAX_CHARS:
        return False
    # An emoji-only message that contains an ack emoji is an ack.
    if _EMOJI_ONLY_RE.match(text.strip()):
        return any(tok in text for tok in _ACK_TOKENS if not tok.isascii())
    stripped = lowered.strip(" .!,")
    if stripped in _ACK_TOKENS:
        return True
    # Leading ack token + short trailer ("perfect, thanks", "cool 👍").
    return any(stripped.startswith(tok) or stripped.endswith(tok) or f" {tok}" in f" {stripped}" for tok in _ACK_TOKENS)


def _is_simple(lowered: str) -> bool:
    """True iff *lowered* asks a DB-answerable status question.

    ``classify`` already returned ``NEEDS_WORK`` for the investigation
    pattern before reaching here, so this only checks the positive tokens.
    """
    return any(tok in lowered for tok in _SIMPLE_QUESTION_TOKENS)


def classify(text: str) -> AnswerRoute:
    """Route *text* to a cheap path; ambiguous ⇒ ``NEEDS_WORK`` (fail-safe)."""
    lowered = _normalized(text)
    if not lowered:
        return AnswerRoute.NEEDS_WORK
    if _has_imperative(lowered):
        return AnswerRoute.NEEDS_WORK
    if _INVESTIGATION_RE.search(lowered):
        return AnswerRoute.NEEDS_WORK
    url_stripped = strip_urls(text)
    lowered_stripped = _normalized(url_stripped)
    if _is_ack(url_stripped, lowered_stripped):
        return AnswerRoute.ACK_ONLY
    if _is_simple(lowered_stripped):
        return AnswerRoute.SIMPLE
    return AnswerRoute.NEEDS_WORK


__all__ = ["AnswerRoute", "classify", "strip_urls"]
