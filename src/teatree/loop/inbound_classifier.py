"""Zero-token routing for one inbound Slack message (#1014).

:func:`classify` is pure logic — no DB, no network, no LLM — so the cheap path can
decide without spending a token. The load-bearing contract is the FAIL-SAFE default:
anything this cannot confidently place routes to :attr:`AnswerRoute.NEEDS_WORK`, so a
real request is never silently swallowed by an acknowledgement or a canned status reply.

Ordering is deliberate and is the whole design:

1. **URLs are stripped first** (:func:`strip_urls`). A shared link carries no intent, but
    its PATH routinely contains ``status`` / ``progress`` / ``today`` / ``ok`` / ``done``.
    Classifying the raw text would read those URL segments as the user asking for a status
    or acknowledging something (#2018). Stripping first means a link-only message has no
    residue left and falls through to NEEDS_WORK, which is what a shared link needs.
2. **Investigation beats question.** ``why … broken/red/wrong/fail`` is phrased as a
    question but wants work, so it is tested before the status branch.
3. **Imperative beats acknowledgement.** "thanks, now fix the build" opens with an ack
    token and is still a request.
4. **A question mark disqualifies an ack** even when an ack token is present.

Only a message whose ENTIRE residue is acknowledgement vocabulary is ACK_ONLY — a
whitelist, not a keyword hit, so "thanks for the link, can you look at it" cannot pass.
"""

import enum
import re


class AnswerRoute(enum.Enum):
    """What the cheap path should do with an inbound message."""

    #: Pure acknowledgement — react, do not reply.
    ACK_ONLY = "ack_only"
    #: Answerable from state the factory already holds (status, open PRs, blockers).
    SIMPLE = "simple"
    #: Needs a real agent. The fail-safe destination for anything ambiguous.
    NEEDS_WORK = "needs_work"


#: Matches a bare or angle-bracketed URL. Slack wraps links as ``<https://…>`` and may
#: append ``|label``; both forms collapse to nothing so only prose survives.
_URL_RE = re.compile(r"<?https?://[^\s>|]+(?:\|[^>]*)?>?", re.IGNORECASE)

#: Emoji that stand alone as an acknowledgement. Deliberately narrow — a thinking face
#: is NOT an ack, it is someone about to say something.
_ACK_EMOJI = frozenset({"👍", "🙏", "👌", "✅", "🎉"})

#: Multi-word acknowledgements, checked before word-level matching.
_ACK_PHRASES = (
    "thank you",
    "got it",
    "will do",
    "sounds good",
    "makes sense",
    "no worries",
    "appreciate it",
    "much appreciated",
)

#: Words that carry no intent of their own. They may pad an acknowledgement ("really
#: appreciate it") but can never CONSTITUTE one — :func:`_is_ack` still requires at least
#: one genuine ack token, so a bare "it" or "that" stays NEEDS_WORK.
_ACK_FILLER = frozenset(
    {"really", "so", "very", "much", "that", "this", "it", "all", "again", "and", "for", "a", "the"}
)

#: Single-word acknowledgement vocabulary.
_ACK_WORDS = frozenset(
    {
        "thanks",
        "thanks!",
        "thx",
        "ty",
        "ok",
        "okay",
        "k",
        "done",
        "great",
        "cool",
        "nice",
        "perfect",
        "lgtm",
        "good",
        "yes",
        "yep",
        "sure",
        "understood",
        "ack",
    }
)

#: Verbs that make a message a request for work regardless of politeness or phrasing.
_IMPERATIVES = (
    "fix",
    "implement",
    "investigate",
    "refactor",
    "debug",
    "add ",
    "change ",
    "remove ",
    "update ",
    "write ",
    "create ",
    "delete ",
    "look into",
    "look at",
    "check ",
    "review ",
    "deploy ",
    "revert ",
    "merge ",
    "rerun",
    "re-run",
)

#: Subjects the factory can answer from its own state without an agent.
_STATUS_TERMS = (
    "status",
    "working on",
    "pending",
    "blocker",
    "blocking",
    "digest",
    "open pr",
    "prs are open",
    "prs open",
    "in flight",
    "in progress",
)

#: ``why … <trouble>`` reads as a question but wants investigation, so it must be tested
#: before the status branch or "why is the status wrong?" would answer itself.
_TROUBLE_TERMS = ("broken", "red", "wrong", "fail", "failing", "failed", "error", "crash", "stuck")


def strip_urls(text: str) -> str:
    """Return *text* with every URL removed.

    Exported because the simple-answer path needs the same residue this classifier
    judged — re-deriving it there with a different regex is how the two drift apart.
    """
    return _URL_RE.sub(" ", text)


def _normalise(text: str) -> str:
    return " ".join(strip_urls(text).lower().split())


def _is_investigation(text: str) -> bool:
    return "why" in text and any(term in text for term in _TROUBLE_TERMS)


def _has_imperative(text: str) -> bool:
    return any(verb in text for verb in _IMPERATIVES)


def _is_ack(text: str) -> bool:
    """True when the residue is acknowledgement vocabulary and nothing else.

    Every token must be an ack token or neutral filler, AND at least one must be a
    genuine ack — so "really appreciate it" passes while a bare "it" or "the thing about
    the other thing" does not.
    """
    residue = text
    matched_phrase = False
    for phrase in _ACK_PHRASES:
        if phrase in residue:
            matched_phrase = True
            residue = residue.replace(phrase, " ")
    tokens = [tok.strip(".,!;:-\"'") for tok in residue.split()]
    tokens = [tok for tok in tokens if tok]
    hits = [tok for tok in tokens if tok in _ACK_WORDS or tok in _ACK_EMOJI]
    if any(tok not in _ACK_WORDS and tok not in _ACK_EMOJI and tok not in _ACK_FILLER for tok in tokens):
        return False
    return matched_phrase or bool(hits)


def classify(text: str) -> AnswerRoute:
    """Route one inbound message, failing safe to :attr:`AnswerRoute.NEEDS_WORK`."""
    normalised = _normalise(text)
    if not normalised:
        # Empty, whitespace-only, or a link with no prose around it.
        return AnswerRoute.NEEDS_WORK
    if _is_investigation(normalised) or _has_imperative(normalised):
        return AnswerRoute.NEEDS_WORK
    # A status request does not need a question mark — "what's the status" and "and
    # pending too" are both asking, and a coalesced turn routinely drops the punctuation.
    if any(term in normalised for term in _STATUS_TERMS):
        return AnswerRoute.SIMPLE
    if "?" in normalised:
        # A question this cannot answer from held state needs a real agent.
        return AnswerRoute.NEEDS_WORK
    if _is_ack(normalised):
        return AnswerRoute.ACK_ONLY
    return AnswerRoute.NEEDS_WORK


__all__ = ["AnswerRoute", "classify", "strip_urls"]
