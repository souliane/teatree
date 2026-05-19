"""Rewrite GitHub-flavored markdown to Slack mrkdwn so links render.

Slack's mrkdwn dialect uses ``<url|label>`` for clickable links. Two
forms commonly leak into messages assembled from regular markdown and
render as inert plain text in the Slack client:

1.  **GitHub-flavored ``[label](url)``** — Slack ignores the brackets and
    shows the literal characters.
2.  **Bare references like ``!281`` or ``#1011``** — Slack has no concept
    of cross-repo issue refs; only an explicit ``<url|!281>`` is clickable.

:func:`slack_linkify` rewrites both forms in place while preserving the
surrounding structure (table pipes, headers, newlines, fenced code).
Token-to-URL resolution is delegated to caller-supplied lookups so the
helper stays overlay-agnostic — ``notify_user`` wires the active
overlay's ``resolve_mr_token`` / ``resolve_issue_token`` hooks when it
applies the transform.
"""

import re
from collections.abc import Callable

TokenResolver = Callable[[int], str | None]

_CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`\n]+`")
_MRKDWN_LINK_RE = re.compile(r"<https?://[^\s|>]+\|[^>]+>")
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
_BARE_MR_RE = re.compile(r"(?<![A-Za-z0-9_/])!(\d+)(?![A-Za-z0-9_])")
_BARE_ISSUE_RE = re.compile(r"(?<![A-Za-z0-9_/])#(\d+)(?![A-Za-z0-9_])")


def slack_linkify(
    text: str,
    *,
    mr_resolver: TokenResolver | None = None,
    issue_resolver: TokenResolver | None = None,
) -> str:
    """Return ``text`` with GH-markdown and bare refs rewritten for Slack.

    *mr_resolver* maps an integer ``N`` to the full URL of merge/pull
    request ``!N`` (or ``None`` when the token is ambiguous across
    repositories — in that case the bare ``!N`` is left untouched so the
    Slack reader sees inert text rather than a wrong link).

    *issue_resolver* does the same for ``#N`` issue tokens.

    Content inside fenced (```` ``` ````) and inline (`` ` ``) code is
    preserved verbatim, matching Slack's own mrkdwn rules. Existing
    Slack mrkdwn links (``<url|label>``) are also preserved, which makes
    the transform idempotent — applying it twice yields the same result.
    """
    if not text:
        return text

    protected: list[str] = []

    def _stash(match: re.Match[str]) -> str:
        protected.append(match.group(0))
        return f"\x00{len(protected) - 1}\x00"

    text = _CODE_FENCE_RE.sub(_stash, text)
    text = _INLINE_CODE_RE.sub(_stash, text)
    text = _MRKDWN_LINK_RE.sub(_stash, text)

    text = _MD_LINK_RE.sub(_rewrite_md_link, text)

    if mr_resolver is not None:
        text = _BARE_MR_RE.sub(_make_token_rewriter("!", mr_resolver), text)
    if issue_resolver is not None:
        text = _BARE_ISSUE_RE.sub(_make_token_rewriter("#", issue_resolver), text)

    def _restore(match: re.Match[str]) -> str:
        return protected[int(match.group(1))]

    return re.sub(r"\x00(\d+)\x00", _restore, text)


def _rewrite_md_link(match: re.Match[str]) -> str:
    label = match.group(1).replace("|", "❘")
    url = match.group(2)
    return f"<{url}|{label}>"


def _make_token_rewriter(sigil: str, resolver: TokenResolver) -> Callable[[re.Match[str]], str]:
    def _rewrite(match: re.Match[str]) -> str:
        n = int(match.group(1))
        url = resolver(n)
        if not url:
            return f"{sigil}{n}"
        return f"<{url}|{sigil}{n}>"

    return _rewrite


__all__ = ["TokenResolver", "slack_linkify"]
