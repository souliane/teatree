"""Rewrite GitHub-flavored markdown to Slack mrkdwn so links render.

Slack's mrkdwn dialect uses ``<url|label>`` for clickable links. Two
forms commonly leak into messages assembled from regular markdown and
render as inert plain text in the Slack client:

1.  **GitHub-flavored ``[label](url)``** — Slack ignores the brackets and
    shows the literal characters.
2.  **Bare references like ``!42`` or ``#1011``** — Slack has no concept
    of cross-repo issue refs; only an explicit ``<url|!42>`` is clickable.

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

#: Maximum rendered line length for an outbound Slack message (#3809). Sized for
#: a phone and a narrow DM column, where a longer line wraps at an arbitrary point.
WRAP_WIDTH = 90

_PLACEHOLDER_RE = re.compile(r"\x00(\d+)\x00")
_LIST_OR_QUOTE_MARKER_RE = re.compile(r"^([-*]\s+|>\s+)")
_NEVER_WRAPPED_PREFIXES = ("#",)
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

    return _restore_placeholders(text, protected)


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

    return _restore_placeholders(text, protected)


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


def wrap_slack_message(text: str, *, width: int = WRAP_WIDTH) -> str:
    """Return *text* with every wrappable line broken to at most *width* chars (#3809).

    Applied at the Slack transport rather than at a composition seam, so a new
    sender inherits the rule instead of having to remember a helper.

    Left intact because breaking them harms readability rather than helping it:
    fenced code, inline code spans, mrkdwn links, bare URLs, table rows,
    headings, and any single whitespace-free token wider than *width*.

    A ``- ``/``* `` bullet's continuation lines are indented under the marker and
    a ``> `` quote's keep the quote prefix, so Slack still renders the structure.

    Idempotent: lines are only ever broken at existing whitespace and never
    rejoined, so every output line is already within *width* (bar the intact
    spans above) and a second application is a no-op. A body with no line to
    break comes back byte for byte, trailing newline and CRLF included: only a
    bare LF is ever treated as a line boundary.
    """
    if not text:
        return text
    protected: list[str] = []
    stashed = _stash_unwrappable(text, protected)
    lines = stashed.split("\n")
    wrapped = [_wrap_line(line, protected, width) for line in lines]
    if wrapped == lines:
        return text
    return _restore_placeholders("\n".join(wrapped), protected)


def slack_line_violations(text: str, *, width: int = WRAP_WIDTH) -> list[str]:
    """Return the lines of *text* that exceed *width* and that wrapping could fix.

    The oracle behind the #3809 acceptance test. It shares
    :func:`wrap_slack_message`'s implementation rather than re-deriving the
    carve-outs, so the test and the transform cannot disagree about what counts
    as a sanctioned exception: a line the wrapper leaves alone is by definition
    one it judged unbreakable.
    """
    if not text:
        return []
    protected: list[str] = []
    stashed = _stash_unwrappable(text, protected)
    return [
        _restore_placeholders(line, protected)
        for line in stashed.split("\n")
        if _display_len(line, protected) > width and _wrap_line(line, protected, width) != line
    ]


def _stash_unwrappable(text: str, protected: list[str]) -> str:
    """Replace every never-broken span with a NUL placeholder, longest form first.

    Order is load-bearing: a mrkdwn link embeds a URL, so stashing links after
    bare URLs would leave the link's own ``|label`` half exposed to the splitter.
    """

    def _stash(match: re.Match[str]) -> str:
        protected.append(match.group(0))
        return f"\x00{len(protected) - 1}\x00"

    for pattern in (_CODE_FENCE_RE, _INLINE_CODE_RE, _MRKDWN_LINK_RE, _BARE_URL_RE):
        text = pattern.sub(_stash, text)
    return text


def _restore_placeholders(text: str, protected: list[str]) -> str:
    """Put every stashed span back, leaving an index we never issued as body text.

    A message may legitimately contain the placeholder sequence; indexing the
    stash with the body's own number raised ``IndexError`` at a transport whose
    callers are contracted a return value, not an exception.
    """

    def _restore(match: re.Match[str]) -> str:
        index = int(match.group(1))
        return protected[index] if index < len(protected) else match.group(0)

    return _PLACEHOLDER_RE.sub(_restore, text)


def _display_len(text: str, protected: list[str]) -> int:
    """Length of *text* as Slack renders it — placeholders count as their content."""
    return len(_restore_placeholders(text, protected))


def _wrap_line(line: str, protected: list[str], width: int) -> str:
    """Break one line at whitespace; return it unchanged when it must not be broken."""
    if _display_len(line, protected) <= width:
        return line
    stripped = line.lstrip()
    # A table row's columns are space-aligned, so any break destroys the alignment.
    if not stripped or stripped.startswith(_NEVER_WRAPPED_PREFIXES) or "|" in line:
        return line
    indent = line[: len(line) - len(stripped)]
    marker, body = _split_list_or_quote_marker(stripped)
    atoms = body.split()
    if not atoms:
        return line
    # A quote's continuation stays quoted; a bullet's aligns under the marker.
    continuation = indent + ("> " if marker.startswith(">") else " " * len(marker))
    out: list[str] = []
    current = indent + marker + atoms[0]
    for atom in atoms[1:]:
        trial = f"{current} {atom}"
        if _display_len(trial, protected) <= width:
            current = trial
        else:
            out.append(current)
            current = continuation + atom
    out.append(current)
    return "\n".join(out)


def _split_list_or_quote_marker(stripped: str) -> tuple[str, str]:
    match = _LIST_OR_QUOTE_MARKER_RE.match(stripped)
    if not match:
        return "", stripped
    return match.group(1), stripped[match.end() :]


__all__ = [
    "WRAP_WIDTH",
    "TokenResolver",
    "normalize_slack_message",
    "slack_line_violations",
    "slack_linkify",
    "wrap_slack_message",
]
