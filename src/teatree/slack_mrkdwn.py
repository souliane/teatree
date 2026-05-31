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

_BULLET_SPLIT_RE = re.compile(r"\s*•\s*")
_EXCESS_BLANK_RE = re.compile(r"\n{3,}")

_PROSE_SPLIT_MIN_LEN = 50
_PROSE_SPLIT_MIN_SENTENCES = 2
_PROSE_SPLIT_MANY_SENTENCES = 3
_INITIAL_TOKEN_LEN = 2
_BARE_URL_RE = re.compile(r"https?://[^\s<>|]+")
_SENTENCE_BREAK_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9*])")
# Lowercased abbreviation guard. Honorifics (Dr./Mr./Mrs./Ms./Prof./St.)
# are deliberately NOT in this set: it is matched case-insensitively, so
# "Mrs." would collide with the merge-request token "MRs." (= merge
# requests) and wrongly suppress the wall-of-text split this transform
# exists to perform. Honorific names are handled separately by a
# case-SENSITIVE guard (see ``_HONORIFICS``).
_ABBREVIATIONS = frozenset(
    {
        "e.g.",
        "i.e.",
        "vs.",
        "etc.",
        "no.",
        "cf.",
        "approx.",
        "fig.",
        "al.",
    }
)
# Case-SENSITIVE honorific guard. Real honorifics carry their natural
# capitalisation ("Dr. Smith"); the merge-request token is written in
# caps ("MR" / "MRs" / "MRs."). A case-sensitive membership test guards
# "Dr. Smith" without re-introducing the "MRs." collision, because
# "MRs." is not == "Mrs.". The split is only suppressed when the
# honorific is immediately followed by a capitalised word (a name).
_HONORIFICS = frozenset({"Dr.", "Mr.", "Mrs.", "Ms.", "Prof.", "St."})


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

    # Stash each rewritten ``[label](url)`` → ``<url|label>`` link immediately:
    # without protection a bare ``#N`` / ``!N`` inside the label (e.g.
    # ``[issue #5](url)``) would be matched by the bare-token resolvers below
    # and corrupted into a nested ``<url|… <url|#5>>`` link.
    def _stash_md_link(match: re.Match[str]) -> str:
        protected.append(_rewrite_md_link(match))
        return f"\x00{len(protected) - 1}\x00"

    text = _MD_LINK_RE.sub(_stash_md_link, text)

    if mr_resolver is not None:
        text = _BARE_MR_RE.sub(_make_token_rewriter("!", mr_resolver), text)
    if issue_resolver is not None:
        text = _BARE_ISSUE_RE.sub(_make_token_rewriter("#", issue_resolver), text)

    def _restore(match: re.Match[str]) -> str:
        return protected[int(match.group(1))]

    return re.sub(r"\x00(\d+)\x00", _restore, text)


def normalize_slack_message(text: str) -> str:
    """Enforce structural readability for outbound Slack mrkdwn messages.

    Transformations applied (code fences, inline code, mrkdwn links and
    bare URLs are preserved verbatim):

    - ``•``-in-paragraph bullets become newline-prefixed ``- `` items,
        one per line, so each renders on its own line in Slack.
    - Bullet groups (consecutive ``- `` lines) are surrounded by blank lines
        to separate them visually from surrounding prose blocks.
    - A multi-sentence single-line prose "wall of text" is split on
        sentence boundaries into blank-line-separated blocks, so
        each idea renders as its own paragraph in Slack rather than one
        unreadable run. Headings, bullets, quotes, table rows and lines
        containing protected spans are never prose-split.
    - Consecutive blank lines (3+) are collapsed to a single blank line.

    The transform is idempotent: applying it twice yields the same result.

    Accepted tradeoff: the sentence-boundary heuristic guards a fixed set of
    common abbreviations and single-capital initials, so a rare unguarded
    abbreviation (followed by a capitalised word) may produce one extra
    paragraph break. This is preferred over leaving long walls of text
    unsplit, which is the defect this transform exists to fix.
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
    text = _BARE_URL_RE.sub(_stash, text)

    text = _normalize_bullets(text)
    text = _surround_bullet_groups(text)
    text = _split_glued_prose(text)
    text = _EXCESS_BLANK_RE.sub("\n\n", text)

    def _restore(match: re.Match[str]) -> str:
        return protected[int(match.group(1))]

    return re.sub(r"\x00(\d+)\x00", _restore, text)


def _normalize_bullets(text: str) -> str:
    """Rewrite ``•``-separated bullet runs into newline-prefixed ``- `` items."""
    lines = text.splitlines(keepends=True)
    result: list[str] = []
    for line in lines:
        trailing_newline = "\n" if line.endswith("\n") else ""
        stripped = line.rstrip("\n")
        if "•" not in stripped:
            result.append(line)
            continue
        parts = _BULLET_SPLIT_RE.split(stripped)
        # parts[0] is the text before the first bullet (may be empty)
        prefix = parts[0].rstrip()
        bullets = [p.strip() for p in parts[1:] if p.strip()]
        if not bullets:
            result.append(line)
            continue
        out_lines: list[str] = []
        if prefix:
            out_lines.append(prefix)
        out_lines.extend(f"- {bullet}" for bullet in bullets)
        result.append("\n".join(out_lines) + trailing_newline)
    return "".join(result)


def _surround_bullet_groups(text: str) -> str:
    """Insert blank lines before and after runs of ``- `` list items."""
    lines = text.splitlines()
    result: list[str] = []
    for i, line in enumerate(lines):
        is_bullet = line.lstrip().startswith("- ")
        prev_is_bullet = i > 0 and lines[i - 1].lstrip().startswith("- ")
        next_is_bullet = i < len(lines) - 1 and lines[i + 1].lstrip().startswith("- ")
        prev_blank = i > 0 and not lines[i - 1]
        next_blank = i < len(lines) - 1 and not lines[i + 1]

        if is_bullet and not prev_is_bullet and not prev_blank and i > 0:
            result.append("")
        result.append(line)
        if is_bullet and not next_is_bullet and not next_blank and i < len(lines) - 1:
            result.append("")

    return "\n".join(result)


def _is_guarded_abbreviation(preceding: str, following: str) -> bool:
    """True if the token before a sentence-break candidate must not split.

    Guards three cases. First, a fixed set of common abbreviations
    (``e.g.``, ``etc.`` …) matched case-insensitively. Second, single
    uppercase initials (``A.``). Third, a case-SENSITIVE honorific
    (``Dr.``, ``Mr.`` …) immediately followed by a capitalised word (a
    name like ``Smith``); the case-sensitive test keeps the all-caps
    merge-request token ``MRs.`` splitting normally because ``"MRs."``
    is not equal to ``"Mrs."``.
    """
    last = preceding.rsplit(None, 1)[-1] if preceding.strip() else ""
    if last.lower() in _ABBREVIATIONS:
        return True
    if len(last) == _INITIAL_TOKEN_LEN and last[0].isupper() and last[1] == ".":
        return True
    next_word = following.split(None, 1)[0] if following.strip() else ""
    return last in _HONORIFICS and bool(next_word) and next_word[0].isupper()


def _split_sentences(line: str) -> list[str]:
    """Split a prose line into sentences, honoring the abbreviation guard."""
    sentences: list[str] = []
    start = 0
    for match in _SENTENCE_BREAK_RE.finditer(line):
        if _is_guarded_abbreviation(line[start : match.start()], line[match.end() :]):
            continue
        sentences.append(line[start : match.start()].strip())
        start = match.end()
    sentences.append(line[start:].strip())
    return [s for s in sentences if s]


def _split_glued_prose(text: str) -> str:
    """Break single-line prose walls into blank-line-separated blocks.

    A line is split only when it is plain prose (not a heading, bullet,
    block quote or table row) **and** the gate below holds:

        sentence_count >= ``_PROSE_SPLIT_MIN_SENTENCES`` (2)
        AND (
            len(line) > ``_PROSE_SPLIT_MIN_LEN`` (50 chars)
            OR sentence_count >= ``_PROSE_SPLIT_MANY_SENTENCES`` (3)
        )

    Rationale: a true "wall of text" is either *long* (a multi-clause
    run that reads as one unbroken paragraph) **or** *many-sentenced*
    (three or more sentences welded onto one line). A terse two-short-
    sentence status line ("Done. Pushed to main now today." — ~31
    chars) is normal dashboard prose, not a wall, so the AND-gate leaves
    it intact while still splitting the genuine multi-sentence walls
    this transform exists to fix. A bare length floor alone would either
    over-split terse lines (floor too low) or leave realistic
    two-sentence walls unsplit (floor too high); pairing the floor with
    the >=3-sentence escape hatch resolves that tension.

    Protected spans (code fences, inline code, mrkdwn links, bare URLs)
    are already NUL-delimited placeholders that contain no sentence
    terminator, so they are opaque to the sentence splitter and survive
    verbatim.
    """
    out: list[str] = []
    for line in text.splitlines(keepends=False):
        stripped = line.lstrip()
        if not stripped or stripped.startswith(("- ", "* ", "> ", "|", "#")):
            out.append(line)
            continue
        sentences = _split_sentences(line)
        if len(sentences) < _PROSE_SPLIT_MIN_SENTENCES:
            out.append(line)
            continue
        is_wall = len(line) > _PROSE_SPLIT_MIN_LEN or len(sentences) >= _PROSE_SPLIT_MANY_SENTENCES
        if not is_wall:
            out.append(line)
            continue
        out.append("\n\n".join(sentences))
    return "\n".join(out)


def _rewrite_md_link(match: re.Match[str]) -> str:
    label = match.group(1).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("|", "❘")
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


__all__ = ["TokenResolver", "normalize_slack_message", "slack_linkify"]
