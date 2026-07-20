"""Spoken-text cleaning — strip markdown / code / URLs / status noise for ``say`` (#277).

The pure text-transform half of :mod:`teatree.core.speak`: turn agent prose
(markdown, code fences, Slack emoji shortcodes, log/status lines) into a capped
plain-text excerpt suitable for the macOS ``say`` binary. No I/O, no config —
just :func:`clean_for_speech` and the line-classifier it drives.
"""

import re

# Speech is throwaway and a long read is worse than no read — a capped
# excerpt keeps ``say`` from droning through a 4 KB status report.
_MAX_SPEAK_CHARS = 600

_CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`]*`")
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((?:[^)]+)\)")
_URL_RE = re.compile(r"https?://\S+")
_HEADING_BULLET_RE = re.compile(r"^\s*(?:#{1,6}|[-*+]|\d+\.)\s+", re.MULTILINE)
_EMPHASIS_RE = re.compile(r"[*_~>|#]")
_WS_RE = re.compile(r"\s+")

# Slack emoji shortcodes (``:information_source:``, ``:white_check_mark:``) are
# status decorations, not prose. ``say`` voices them letter-soup
# (``:information_source:`` reads as "information source"), so they are dropped
# wholesale so they never reach the speakers as gibberish.
_EMOJI_SHORTCODE_RE = re.compile(r":[a-z0-9_+-]+:", re.IGNORECASE)

# A leading emoji shortcode is a status decoration — a line that STARTS with one
# is a status line (the notify ``:information_source: *info*`` prefix, a
# ``:white_check_mark: test green`` check). Real prose never opens a line with a
# ``:shortcode:``.
_LEADING_EMOJI_RE = re.compile(r"^\s*(?::[a-z0-9_+-]+:\s*)+", re.IGNORECASE)

# After markdown stripping, the notify kind marker (``*info*`` -> ``info``) is a
# bare status word. These three are the only notify kinds (``NotifyKind``), so a
# line that is ONLY one of them is the marker preamble, never a real message.
_NOISE_KIND_MARKERS = frozenset({"info", "answer", "question"})

# A log line leads with a level token AND a genuine log discriminator —
# ``INFO: ...``, ``[DEBUG] ...``, ``WARNING - ...``, ``Notice: ...``. Two shapes:
# a BRACKETED level (``[DEBUG]`` — the closing bracket is the discriminator), or
# a bare level immediately followed by a ``:`` or ``-`` separator. The
# discriminator is REQUIRED: without it the leading word is ordinary prose that
# merely happens to start with a level token ("Warning users now about the
# outage", "Critical bug found in prod.") and must stay spoken. Anchored to the
# line START so the same words mid-sentence ("the info you asked for") stay prose.
_LOG_LEVEL_LINE_RE = re.compile(
    # bracketed level (``[DEBUG]`` — closing bracket is the discriminator) ...
    r"^(?:\[\s*(?:trace|debug|info|notice|warn|warning|error|critical|fatal)\s*\]\s*[:\-]?\s+\S"
    # ... or a bare level immediately followed by a required ``:`` / ``-`` separator.
    r"|(?:trace|debug|info|notice|warn|warning|error|critical|fatal)\s*[:\-]\s+\S)",
    re.IGNORECASE,
)

# Sentence-ending punctuation. A terse status fragment ("test green") has none;
# a real emoji-led message ("We shipped the release!") does, so it is kept.
_SENTENCE_END_RE = re.compile(r"[.!?]")


def clean_for_speech(text: str) -> str:
    """Strip markdown / code / URLs / status noise and cap length so ``say`` reads prose.

    Code fences and inline code are dropped entirely (reading source aloud
    is noise); a ``[label](url)`` markdown link collapses to its label; a
    bare URL is dropped; Slack emoji shortcodes (``:information_source:``) are
    dropped; heading/bullet/emphasis sigils are removed. Whole LOG/STATUS lines
    are then filtered out (#277): a line that is only a notify kind marker
    (``*info*``/``*answer*``/``*question*``) or that leads with a log level
    (``INFO:``/``[DEBUG]``) is droning preamble — ``say`` reads it as
    "Info source", "Info test green" before the real message — so it is
    dropped, while a real message that merely CONTAINS those words inline is
    kept. Surviving lines are joined, runs of whitespace collapse to a single
    space, and the result is truncated to :data:`_MAX_SPEAK_CHARS` on a word
    boundary with a trailing ``…``.
    """
    stripped = _CODE_FENCE_RE.sub(" ", text)
    stripped = _INLINE_CODE_RE.sub(" ", stripped)
    stripped = _MD_LINK_RE.sub(r"\1", stripped)
    stripped = _URL_RE.sub(" ", stripped)
    stripped = _HEADING_BULLET_RE.sub("", stripped)
    # Filter whole status/log lines BEFORE the emoji strip, so a leading emoji
    # shortcode is still visible to the line discriminator.
    kept = [line for line in stripped.splitlines() if not _is_noise_line(line)]
    stripped = "\n".join(kept)
    stripped = _EMOJI_SHORTCODE_RE.sub(" ", stripped)
    stripped = _EMPHASIS_RE.sub("", stripped)
    stripped = _WS_RE.sub(" ", stripped).strip()
    if len(stripped) <= _MAX_SPEAK_CHARS:
        return stripped
    head = stripped[:_MAX_SPEAK_CHARS].rsplit(" ", 1)[0].rstrip()
    return f"{head}…"


def _is_noise_line(line: str) -> bool:
    """Whether ``line`` is a log/status marker rather than user-facing prose (#277).

    Run BEFORE the emoji strip (so a leading ``:shortcode:`` is still visible).
    A line is status/log noise in any of three cases:

    *   **Notify kind marker** — once its emphasis sigils are gone the line is
        only ``info``/``answer``/``question`` (the three ``NotifyKind`` values),
        the DM preamble :func:`teatree.core.notify.format_notification` prepends.
    *   **Log level line** — it leads with a level token (``INFO:``, ``[DEBUG]``,
        ``WARNING -``) as a distinct leading token. The same words mid-sentence
        ("the info you asked for") are prose and survive — the level must LEAD.
    *   **Emoji-led terse status** — it opens with a ``:shortcode:`` AND, with the
        emoji and sigils removed, carries no sentence-ending punctuation
        (``:white_check_mark: test green``). A real emoji-led message ends a
        sentence ("We shipped the release!") and is kept.

    Blank lines are never noise (they collapse away in the whitespace pass).
    """
    candidate = line.strip()
    if not candidate:
        return False
    bare = _EMPHASIS_RE.sub("", _EMOJI_SHORTCODE_RE.sub(" ", candidate)).strip()
    if bare.lower() in _NOISE_KIND_MARKERS:
        return True
    if _LOG_LEVEL_LINE_RE.match(bare):
        return True
    led_with_emoji = _LEADING_EMOJI_RE.match(candidate) is not None
    return led_with_emoji and not _SENTENCE_END_RE.search(bare)
