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

from teatree.hooks.quote_scanner import ScanResult


@dataclass(frozen=True)
class QuoteGateSurface:
    """The clauses of a HIGH-match deny reason that differ between arms."""

    gate_label: str
    carrier: str
    consequence: str
    escape_location: str


#: The ``Agent``/``Task`` ``PreToolUse`` arm (#1401) — the only interception point a
#: sub-agent dispatch has. Byte-frozen: this wording is correct and is ridden by the
#: never-lockout contract, the liveness corpus and the deny-circuit leak family.
DISPATCH_SURFACE: Final[QuoteGateSurface] = QuoteGateSurface(
    gate_label="pre-dispatch quote-scanner gate (#1401)",
    carrier="The Agent/Task prompt",
    consequence="before dispatching (the sub-agent would otherwise echo it into a published output, "
    "defeating the #1213 publish gate)",
    escape_location="near the start of the prompt",
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
        f"If the match is a false positive, add `[quote-ok: <reason>]` {surface.escape_location}."
    )


def format_block_message(result: ScanResult) -> str:
    """Render the publish-boundary deny reason for a HIGH match (#1213).

    The false-positive escape names the leading ``QUOTE_OK=1`` env PREFIX, not a
    ``--quote-ok`` CLI flag: the flag is consumed by the gate's parser, never by
    the posting command, so a ``t3 review post-comment`` (or any other
    subcommand) would reject it as an unknown option. The env prefix is a real
    shell construct every command accepts and is the spelling that actually
    works at the prompt.
    """
    names = ", ".join(sorted({f.name for f in result.high}))
    return (
        "BLOCKED: pre-publish quote-scanner gate (#1213). "
        f"Matched patterns: {names}. "
        "Paraphrase any user-attributed content; do not quote verbatim. "
        "If the match is a false positive, re-issue the command with a leading "
        "QUOTE_OK=1 env prefix (e.g. `QUOTE_OK=1 <command>`)."
    )


def format_dispatch_block_message(result: ScanResult) -> str:
    """Render the PreToolUse deny reason for a HIGH match in a dispatch prompt (#1401)."""
    return _format_quote_block_message(result, DISPATCH_SURFACE)


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
