"""Operator-facing reasons for the quote-scanner gate's verdicts.

``quote_scanner`` is pure detection — it answers "does this text have the shape of
a quotation?" and nothing about who is being told. Rendering the reason is the
other concern, and it is the one that has to know WHICH surface produced the
verdict: one detector governs several unrelated surfaces, so a single shared
sentence is wrong on whichever surface it does not describe, and a reason that
misnames the carrier sends the reader to a remedy that does not exist there
(#4381). Splitting it out is what makes a surface a row of data rather than a
fourth copy of the sentence.

Imports ``quote_scanner`` for its result type and is never imported back, so the
detector stays free of presentation.
"""

from dataclasses import dataclass
from typing import Final

from teatree.hooks import _dispatch_quote_ok
from teatree.hooks.quote_scanner import ScanResult


@dataclass(frozen=True)
class QuoteGateSurface:
    """The clauses of a HIGH-match deny reason that differ between arms."""

    gate_label: str
    carrier: str
    consequence: str
    escape_location: str


#: The ``Agent``/``Task`` ``PreToolUse`` arm (#1401) — the only interception point a
#: sub-agent dispatch has. Kept only so :class:`TestTheSurfacesAreDistinct` can prove
#: no two arms share a clause set; the ACTUAL dispatch message is rendered by
#: :mod:`teatree.hooks._dispatch_quote_ok` (:func:`format_dispatch_block_message`),
#: whose wording is byte-frozen and ridden by the never-lockout contract, the
#: liveness corpus and the deny-circuit leak family.
DISPATCH_SURFACE: Final[QuoteGateSurface] = QuoteGateSurface(
    gate_label="pre-dispatch quote-scanner gate (#1401)",
    carrier="The Agent/Task prompt",
    consequence="before dispatching (the sub-agent would otherwise echo it into a published output, "
    "defeating the #1213 publish gate)",
    escape_location="in the one-line `description` (subject) field",
)

#: The task-list arm (#171). The task-list tools bypass ``PreToolUse`` entirely and
#: their event has one producer, so this arm scans an ENTRY's own text and never sees
#: a dispatch — hence its own carrier, consequence and escape location.
TASK_ENTRY_SURFACE: Final[QuoteGateSurface] = QuoteGateSurface(
    gate_label="task-entry quote-scanner gate (#171)",
    carrier="The task subject/description",
    consequence="before the entry is created (it would otherwise sit in the task list and be echoed "
    "into a published output, defeating the #1213 publish gate)",
    escape_location="near the start of the task subject or description",
)


def _format_quote_block_message(result: ScanResult, surface: QuoteGateSurface) -> str:
    """Render a HIGH-match deny reason in the terms of the surface that produced it."""
    names = ", ".join(sorted({f.name for f in result.high}))
    excerpt = next((f.excerpt for f in result.high if f.excerpt), "")
    matched = f' (e.g. "{excerpt}")' if excerpt else ""
    return (
        f"BLOCKED: {surface.gate_label}. {surface.carrier} "
        f"carries verbatim user-voice/PII content{matched} — matched patterns: {names}. "
        f"Paraphrase it into author-voice description {surface.consequence}. "
        "If this is a false match, rephrase without the quoted span or ask the owner. "
        "The owner may approve this one entry with a per-call `[quote-ok: <reason>]` override "
        f"{surface.escape_location}."
    )


def format_block_message(result: ScanResult, *, slack_mcp: bool = False) -> str:
    """Render the publish-boundary deny reason for a HIGH match (#1213).

    Name the per-call override only as an option for the owner to approve.
    """
    names = ", ".join(sorted({f.name for f in result.high}))
    carrier = "The scan matched quoted owner text" if slack_mcp else "The publish body matched quoted owner text"
    return (
        f"BLOCKED: pre-publish quote-scanner gate (#1213). {carrier}; matched patterns: {names}. "
        "Rephrase without the quoted span or ask the owner if this is a false match. "
        "Ask the owner to review the blocked publication."
    )


def format_dispatch_block_message(result: ScanResult) -> str:
    """Render the PreToolUse deny reason for a HIGH match in a dispatch prompt (#1401)."""
    names = ", ".join(sorted({f.name for f in result.high}))
    excerpt = next((f.excerpt for f in result.high if f.excerpt), "")
    return _dispatch_quote_ok.block_message(names, excerpt)


def format_task_entry_block_message(result: ScanResult) -> str:
    """Render the task-list deny reason for a HIGH match in a task entry's text (#171)."""
    return _format_quote_block_message(result, TASK_ENTRY_SURFACE)


def format_warn_message(result: ScanResult) -> str:
    """Render the stderr warning for a MEDIUM-only match."""
    names = ", ".join(sorted({f.name for f in result.medium}))
    return (
        f"WARNING: pre-publish quote-scanner gate (#1213) — attribution patterns matched ({names}). "
        "Verify the content is paraphrased, not lifted from user speech."
    )
