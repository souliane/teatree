"""Scan a PR body or commit message for AI-signature / banned trailers.

Enforces the "No AI Signature on Posts Made on the User's Behalf" rule
(BLUEPRINT §17.6 gate 15, #836) as deterministic code. The rule lived
only as prose in ``/t3:rules`` and was unenforced at the PR-body /
commit-message layer — PR #831 leaked the ``Generated with [Claude
Code]`` trailer, caught only by cold review.

Used by: the ``ai-sig-scan`` tool command (``scripts/ai_signature_scan.py``),
the pr-create-time hook gate, and the bound-merge message gate
(``teatree.core.gates.merge_message_gate``).

Design — match trailer *position*, not a bare substring. A banned
pattern only trips when it appears as a *footer/trailer line*: the
match must start at the beginning of the line after stripping leading
whitespace AND leading markdown markers (blockquote ``>``/``>>``, list
``-``/``*``/``+``) — otherwise a quoted-reply or list-item prefix
smuggles the trailer past the anchor (cold-review finding 1). Prose
that *describes* the banned trailer ("a ``Generated with [Claude
Code]`` footer") does not start the line with the pattern (it is
preceded by prose and/or wrapped in backticks), so the rule's own
documentation, /t3:rules, and BLUEPRINT do not self-block. A line whose
stripped form is fully inside an inline-code span is also exempt. The
``via/using/with claude`` footer is anchored at the line start (no
arbitrary preceding prose) so legitimate body prose such as "Reviewed
the design with Claude" passes (cold-review finding 3).
"""

import re

# Each pattern is anchored at the START of the stripped line (trailer
# position). ``re.IGNORECASE`` so ``Co-authored-by`` / ``CO-AUTHORED-BY``
# variants are caught. The Co-Authored-By trailer is banned only when it
# names a model / Claude / Anthropic — a human co-author is legitimate.
_MODEL_AUTHOR = r"(?:claude|anthropic|gpt|opus|sonnet|haiku|copilot|\bai\b|noreply@anthropic)"

# Every agent the rule's published list names as the subject of an attribution
# footer — ``via Claude``, ``(via AI)``, ``via the assistant`` are one shape.
_AGENT_NOUN = r"(?:claude\b|the assistant\b|an?\s+ai\b|ai\b)"

_TRAILER_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "co-authored-by-model",
        re.compile(rf"^co-authored-by:\s*.*{_MODEL_AUTHOR}", re.IGNORECASE),
    ),
    (
        "generated-with",
        re.compile(r"^(?:\U0001f916\s*)?generated with\b", re.IGNORECASE),
    ),
    (
        "generated-with-claude-code",
        re.compile(r"^(?:\U0001f916\s*)?generated with \[claude code\]", re.IGNORECASE),
    ),
    (
        # Footer-position only: the line *is* the attribution footer —
        # ``via Claude``, optionally led by a single
        # generated/sent-style verb (``Generated with Claude``). No
        # arbitrary preceding prose, so legitimate body text like
        # "Reviewed the design with Claude" passes (finding 3).
        "via-agent",
        re.compile(
            rf"^(?:(?:generated|sent|posted|created|written|drafted)\s+)?(?:via|using|with) {_AGENT_NOUN}\s*$",
            re.IGNORECASE,
        ),
    ),
    (
        "sent-using-agent",
        re.compile(rf"^(?:sent|posted|created|written|drafted) (?:using|via|with|by) {_AGENT_NOUN}", re.IGNORECASE),
    ),
    (
        "written-by-agent",
        re.compile(
            r"^this (?:message|comment|post|reply|note|text|description|body|pr|mr) "
            r"was (?:written|generated|drafted|created|composed|authored) by\b",
            re.IGNORECASE,
        ),
    ),
    (
        "ai-generated",
        re.compile(r"^ai[-\s]generated\b", re.IGNORECASE),
    ),
    (
        "emoji-bot-footer",
        re.compile(r"^\U0001f916\s*\S"),
    ),
    (
        # Whole-line only, so body prose that merely mentions an assistant or AI passes.
        "via-the-assistant",
        re.compile(r"^\(?via (?:ai|the assistant)\)?[.!]?\s*$", re.IGNORECASE),
    ),
    (
        "ai-generated",
        re.compile(r"^\(?ai[- ]generated\)?[.!]?\s*$", re.IGNORECASE),
    ),
    (
        "written-by-footer",
        re.compile(r"^this message was written by\b", re.IGNORECASE),
    ),
]


# Leading markdown markers that must be peeled before anchoring the
# trailer match: one or more blockquote ``>`` and/or a single list
# bullet (``-``/``*``/``+``), each optionally followed by whitespace,
# in any leading combination (``> - ``, ``>> ``, ``- ``). Repeated so
# ``>> via Claude`` and ``> - Co-Authored-By: …`` are both unwrapped.
_MD_PREFIX_RE = re.compile(r"^(?:\s*(?:>+|[-*+])\s*)+")

# A footer the rule publishes parenthesised (``(via AI)``) is the same footer:
# the wrapper is what defeats the line-leading anchor and the ``$`` tail.
_WRAPPER_RE = re.compile(r"^[(\[{]+\s*|\s*[)\]}]+$")


def _strip_wrapping_delimiters(line: str) -> str:
    """Remove enclosing brackets so a parenthesised footer still anchors."""
    return _WRAPPER_RE.sub("", line)


def _strip_markdown_prefix(line: str) -> str:
    """Remove a leading blockquote/list-item markdown prefix.

    A quoted reply (``> Co-Authored-By: …``) or a list item (``-
    Generated with …``) is the same trailer in footer position; the
    markdown marker must not let it slip past the line-leading anchor
    (cold-review finding 1). Only *leading* markers are peeled — a ``-``
    inside running prose is untouched.
    """
    return _MD_PREFIX_RE.sub("", line)


def _strip_inline_code(line: str) -> str:
    """Blank out inline-code spans.

    A backticked *mention* of the trailer (documentation describing the
    banned pattern) must not trip the gate. The text between paired
    backticks is replaced with spaces of equal length so column offsets
    are preserved but the banned pattern inside a code span no longer
    matches as a line-leading trailer.
    """
    return re.sub(r"`[^`]*`", lambda m: " " * len(m.group()), line)


def scan_text(text: str) -> list[tuple[int, str, str]]:
    """Return ``(lineno, category, matched_line)`` for every banned trailer.

    A line is only a finding when, after blanking inline-code spans,
    stripping leading whitespace, and peeling any leading
    blockquote/list markdown prefix, a banned pattern matches at the
    line start — i.e. the banned text is in trailer/footer position, not
    described in running prose. Enclosing brackets are peeled last, so a
    parenthesised footer anchors like its bare twin.
    """
    findings: list[tuple[int, str, str]] = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        candidate = _strip_wrapping_delimiters(_strip_markdown_prefix(_strip_inline_code(raw).strip()).strip()).strip()
        if not candidate:
            continue
        for category, pattern in _TRAILER_PATTERNS:
            if pattern.match(candidate):
                findings.append((lineno, category, raw.strip()))
                break
    return findings


def summary(findings: list[tuple[int, str, str]]) -> str:
    if not findings:
        return "AI-signature scan: clean (0 findings)"
    header = f"AI-signature scan: {len(findings)} banned trailer(s)"
    rows = [f"  line {ln}: {cat}: {text}" for ln, cat, text in findings]
    return "\n".join([header, *rows])
