"""Canonical inline ``#N (short title)`` reference renderer (#2092).

A user directive: every surface that lists a ticket/MR/PR/issue id must render
the human-readable title inline — never a bare ``#N`` nor a link-only id the
reader can't interpret. The complement of :mod:`teatree.core.reference_linkifier`
(which resolves a bare ref to a clickable *URL*); this module owns the *title*
half of the contract — the short ``(topic)`` chunk that tells the reader what
``#N`` actually is.

One chokepoint, so every listing surface (``/checking``, ``/todos``, notify
recaps) formats identically. The truncation rule (drop the conventional-commit
prefix, keep a terse few-word topic) is the same one the loop statusline applies
in :func:`teatree.loop.rendering_items._short_desc`, lifted here so both share a
single definition rather than re-implementing the budget per surface.
"""

import re
from typing import Final

#: Topic word budget for the inline ``(title)`` chunk — a glance-target, not a
#: changelog. Matches the statusline chip budget so every surface reads alike.
_TITLE_WORDS: Final[int] = 6
_TITLE_MAX_LEN: Final[int] = 48

#: Conventional-commit / scoped prefix (``fix:``, ``feat(loop):``,
#: ``techdebt:``) carries no topic signal in a listing — every row is already
#: work-in-flight — so it is stripped before the word budget is applied.
_CC_PREFIX_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z][\w-]*(?:\([^)]*\))?!?:\s*", re.IGNORECASE)


def short_title(title: str) -> str:
    """Return a terse ``≤6``-word topic for *title* (the inline ``(title)`` chunk).

    Empty input → empty output (the caller suppresses the ``(title)`` chunk).
    The leading conventional-commit prefix is dropped, then the first
    :data:`_TITLE_WORDS` words are kept and capped at :data:`_TITLE_MAX_LEN`
    chars with a single-codepoint Unicode ellipsis.
    """
    if not title:
        return ""
    stripped = _CC_PREFIX_RE.sub("", title, count=1).strip()
    if not stripped:
        stripped = title.strip()
    words = stripped.split()
    topic = " ".join(words[:_TITLE_WORDS])
    if len(topic) > _TITLE_MAX_LEN:
        return topic[: _TITLE_MAX_LEN - 1].rstrip() + "…"
    return topic


def render_ref(label: str, *, title: str = "", url: str = "") -> str:
    """Render one id reference as ``#N (short title)`` — never a bare/link-only id.

    *label* is the already-formed id token (``#42``, ``TODO-7``, ``acme/x#3``).
    *title* is the human-readable description; it is truncated by
    :func:`short_title` and appended as `` (topic)`` when non-blank. *url*, when
    given, wraps the **whole** ``label (title)`` in a markdown link so the title
    is inside the clickable text — a reader sees ``[#42 (fix the widget)](url)``,
    not a clickable number next to an unlinked title.

    The title chunk is suppressed when *title* is blank, so a row with no known
    title degrades to the plain (still-clickable when *url* is set) id rather
    than an empty ``()``.
    """
    topic = short_title(title)
    inner = f"{label} ({topic})" if topic else label
    return f"[{inner}]({url})" if url else inner


__all__ = ["render_ref", "short_title"]
