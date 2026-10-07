"""Forge-neutral review-comment checks, shared by ``review post-comment`` and the findings publish.

The predicates are the policy. The ``teatree.cli.review`` gates wrap them with their per-call
escapes and steering text; :func:`comment_refusal` composes them, in the same order, for a
published colleague review, which has no escapes.
"""

import re
from dataclasses import dataclass

from teatree.core.backend_protocols import PrReview

COLLEAGUE_PROSE_CAP_PARAGRAPHS = 3
COLLEAGUE_PROSE_CAP_WORDS = 200
MIN_DISTINCT_FINDINGS = 2

# An ``@`` preceded by a word character is an email or an infix, never a person.
_HANDLE_RE = re.compile(r"(?<![\w.])@[A-Za-z][\w.-]{1,}\b")

_CODE_SPAN_RE = re.compile(r"```.*?```|`[^`\n]+`", re.DOTALL)

# Gherkin / Playwright scenario tags a review of a feature file legitimately names.
_GHERKIN_TAGS: frozenset[str] = frozenset(
    {
        "@automated",
        "@awaiting-merge",
        "@blocked",
        "@critical",
        "@e2e",
        "@fixme",
        "@flaky",
        "@ignore",
        "@manual",
        "@only",
        "@ready",
        "@regression",
        "@sanity",
        "@serial",
        "@skip",
        "@slow",
        "@smoke",
        "@wip",
    }
)

# Slack's ``ts`` shape; a ratio or a version has far fewer digits.
_SLACK_TS_RE = re.compile(r"\b\d{10}\.\d{6}\b")

# Two or more digits, so a heading ``#`` or a single-digit footnote does not register.
_TICKET_REF_RE = re.compile(r"(?<![\w/])[#!]\d{2,}\b")

# Social coordination turns a tracker id into chatter; a bare ``tracked at #1234`` stays legitimate.
_COORDINATION_RE = re.compile(
    r"\b(?:"
    r"ping(?:\s+the)?\b|"
    r"sync(?:\s+(?:up|with))?\b|"
    r"reach\s+out\b|"
    r"loop\s+in\b|"
    r"coordinate\s+with\b|"
    r"check\s+with\b|"
    r"ask\s+(?:the\s+)?(?:author|team)\b|"
    r"(?:in|at|during)\s+standup\b|"
    r"the\s+(?:wider\s+)?team\b|"
    r"the\s+author\s+should\b"
    r")",
    re.IGNORECASE,
)

# The ``.ext`` is required and is letters only, so ``ratio 3:2`` or ``12:30`` does not match.
_FILE_LINE_RE = re.compile(r"\b[\w./-]+\.[A-Za-z]{1,8}:\d+\b")

_NUMBERED_ITEM_RE = re.compile(r"^\s*\d+[.)]\s+.*\b[\w./-]+\.[A-Za-z]{1,8}\b", re.MULTILINE)

# Biased to flag: a false positive costs one evidence record, a false negative recurs #1280.
_EVIDENCE_CLAIM_RE = re.compile(
    r"\b(?:"
    r"is\s+(?:missing|wrong|broken|stale|incorrect)|"
    r"are\s+(?:missing|wrong|broken|stale|incorrect)|"
    r"does\s+not\s+exist|"
    r"do\s+not\s+exist|"
    r"doesn'?t\s+exist|"
    r"don'?t\s+exist|"
    r"cannot\s+find|"
    r"can'?t\s+find|"
    r"there\s+is\s+no\s+(?:such\s+)?[a-z_]+|"
    r"should\s+not\s+exist|"
    r"shouldn'?t\s+exist|"
    r"no\s+such\s+(?:function|method|symbol|file|module|helper|class)|"
    r"missing\s+from|"
    r"stale\s+reference"
    r")\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class ProseCapBreach:
    breach: str
    cap: str


def _count_paragraphs(body: str) -> int:
    text = body.strip()
    if not text:
        return 0
    return sum(1 for chunk in text.split("\n\n") if chunk.strip())


def _count_words(body: str) -> int:
    return len(body.split())


def prose_cap_breach(body: str) -> ProseCapBreach | None:
    """The colleague prose cap *body* exceeds, paragraphs first, or ``None``."""
    paragraph_count = _count_paragraphs(body)
    if paragraph_count > COLLEAGUE_PROSE_CAP_PARAGRAPHS:
        return ProseCapBreach(f"{paragraph_count}-paragraph", f"{COLLEAGUE_PROSE_CAP_PARAGRAPHS}-paragraph cap")
    word_count = _count_words(body)
    if word_count > COLLEAGUE_PROSE_CAP_WORDS:
        return ProseCapBreach(f"{word_count}-word", f"{COLLEAGUE_PROSE_CAP_WORDS}-word cap")
    return None


def references_project_chatter(body: str) -> bool:
    """Whether *body* names a stakeholder, quotes a Slack ``ts``, or pairs a tracker id with coordination."""
    if not body:
        return False
    if _names_a_stakeholder(body) or _SLACK_TS_RE.search(body):
        return True
    return bool(_TICKET_REF_RE.search(body) and _COORDINATION_RE.search(body))


def _names_a_stakeholder(body: str) -> bool:
    prose = _CODE_SPAN_RE.sub(lambda match: " " * len(match.group(0)), body)
    return any(match.group(0).lower() not in _GHERKIN_TAGS for match in _HANDLE_RE.finditer(prose))


def inline_findings_count(body: str) -> int:
    """How many per-line findings *body* reads as: distinct ``path.ext:line`` cites or numbered file items."""
    cites = {match.group(0) for match in _FILE_LINE_RE.finditer(body)}
    return max(len(cites), sum(1 for _ in _NUMBERED_ITEM_RE.finditer(body)))


def looks_like_inline_findings(body: str) -> bool:
    return bool(body) and inline_findings_count(body) >= MIN_DISTINCT_FINDINGS


def evidence_claim_phrase(body: str) -> str:
    """The first "X is missing/wrong/broken" phrase in *body*, or ``""``."""
    match = _EVIDENCE_CLAIM_RE.search(body) if body else None
    return match.group(0) if match else ""


def looks_like_evidence_claim(body: str) -> bool:
    return bool(evidence_claim_phrase(body))


def comment_refusal(body: str, *, general: bool) -> str:
    """The first check *body* fails as an escape-free colleague comment, or ``""``.

    The multi-finding check reads a *general* note only: an inline comment already sits on its line.
    """
    breach = prose_cap_breach(body)
    if breach is not None:
        return f"colleague prose cap: the {breach.breach} body exceeds the {breach.cap}"
    if references_project_chatter(body):
        return "comment bloat: it carries project chatter (an @handle, a Slack ts, or a tracker id with coordination)"
    count = inline_findings_count(body)
    if general and count >= MIN_DISTINCT_FINDINGS:
        return f"multi-finding general note: {count} file:line findings in one note need one inline comment each"
    phrase = evidence_claim_phrase(body)
    if phrase:
        return f"unbacked claim: {phrase!r} asserts something is missing or wrong without FindingEvidence receipts"
    return ""


def review_refusal(review: PrReview) -> str:
    """The first check any body of *review* fails, prefixed with which body, or ``""``."""
    bodies = [
        ("the summary", review.body, True),
        *((f"the comment on {item.path}:{item.line}", item.body, False) for item in review.comments),
    ]
    for where, body, general in bodies:
        refusal = comment_refusal(body, general=general)
        if refusal:
            return f"{where} — {refusal}"
    return ""
