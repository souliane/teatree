"""The press-review digest, rendered for Slack (#3669).

The source system delivered its aggregated review to Slack; so does this one. The
owner's standing Slack directive is non-negotiable and applies here: **clickable
links, and code symbols / file paths in monospace.** A digest of inert
``[title](url)`` text and bare ``src/…`` paths satisfies neither.

Links go through :func:`~teatree.slack_mrkdwn.slack_linkify` (the single mrkdwn
rewriter — markdown links become ``<url|label>``, existing spans are preserved so
the transform stays idempotent). Code spans are applied HERE, before linkify, so
the backticked spans are already protected when the link rewriter runs.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass

from teatree.core.code_tokens import rewrite_code_tokens
from teatree.slack_mrkdwn import normalize_slack_message, slack_linkify

_CODE_SPAN_RE = re.compile(r"`[^`\n]+`")


@dataclass(frozen=True, slots=True)
class DigestItem:
    """One candidate the scan surfaced, as it appears in the digest."""

    title: str
    url: str
    rationale: str


def format_code_symbols(text: str) -> str:
    """Wrap bare file paths and dotted symbols in backticks, leaving spans and URLs alone.

    Detection is the shared :func:`~teatree.core.code_tokens.rewrite_code_tokens`
    the dashboard also uses — only the wrapping differs, so the two surfaces can
    never disagree about what a code token is.
    """
    return rewrite_code_tokens(text, lambda token: f"`{token}`", protected=(_CODE_SPAN_RE,))


def render_digest(items: Sequence[DigestItem], *, scanned_on: str) -> str:
    """The Slack-ready press review, or ``""`` when the scan surfaced nothing.

    An empty scan renders nothing at all rather than a "0 items" DM — a daily
    no-op notification is noise the owner did not ask for.
    """
    if not items:
        return ""
    lines = [
        f"Press review — {scanned_on} — {len(items)} candidate(s) queued behind the ask-gate.",
        "Nothing is filed as an issue until you approve.",
        "",
    ]
    lines.extend(f"- [{item.title}]({item.url}) — {format_code_symbols(item.rationale)}" for item in items)
    return slack_linkify(normalize_slack_message("\n".join(lines)))


__all__ = ["DigestItem", "format_code_symbols", "render_digest"]
