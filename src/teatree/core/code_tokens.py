"""Where a file path or a code symbol sits in prose — one detector, two surfaces (#3624).

The owner's standing formatting directive — clickable links, and code symbols and
file paths in monospace — applies to every surface the factory writes to. The
Slack digest (:mod:`teatree.core.news_digest`) renders those tokens as backticks
and the dashboard renders them as ``<code>``, but a token is a token: detecting
them twice would let the two surfaces disagree about what counts as one.

Only the WRAPPING differs, so only the wrapping is passed in.
"""

import re
from collections.abc import Callable, Sequence

#: A bare URL. Always protected — a URL contains both a ``/``-path and dots, so an
#: unprotected rewrite would shred every link into code spans.
URL_RE = re.compile(r"https?://\S+")

#: A path-like token: at least one ``/`` and a file extension (``src/a/b.py``).
_PATH_RE = re.compile(r"(?<![\w`/])(?:[\w.-]+/)+[\w-]+\.[A-Za-z0-9]{1,6}(?![\w`])")
#: A dotted symbol: three or more identifier segments (``teatree.core.tasks.claim``).
_SYMBOL_RE = re.compile(r"(?<![\w`.])[a-z_][\w]*(?:\.[a-z_][\w]*){2,}(?![\w`])")

_PLACEHOLDER_RE = re.compile(r"\x00(\d+)\x00")


def rewrite_code_tokens(
    text: str,
    wrap: Callable[[str], str],
    *,
    protected: Sequence[re.Pattern[str]] = (),
) -> str:
    """Return *text* with every code token passed through *wrap*.

    Spans matching *protected* (plus :data:`URL_RE`, always) are stashed before
    the rewrite and restored after, so the transform never reaches inside a link
    or an existing code span — which is what makes it idempotent.
    """
    stashed: list[str] = []

    def _stash(match: re.Match[str]) -> str:
        stashed.append(match.group(0))
        return f"\x00{len(stashed) - 1}\x00"

    for pattern in (*protected, URL_RE):
        text = pattern.sub(_stash, text)
    for pattern in (_PATH_RE, _SYMBOL_RE):
        text = pattern.sub(lambda m: wrap(m.group(0)), text)
    return _PLACEHOLDER_RE.sub(lambda m: stashed[int(m.group(1))], text)


__all__ = ["URL_RE", "rewrite_code_tokens"]
