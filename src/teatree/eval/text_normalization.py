"""Conservative normalization for matcher-visible assistant prose."""

import re

# Assistant prose is Markdown, while behavioral matchers grade its words. Inline
# emphasis around one lexical token (``**not**``, ``_unverified_``) must not turn
# a semantic predicate into a formatting lottery. Keep this deliberately narrow:
# code spans, paths/URLs, punctuation, multi-word spans, and tool arguments are untouched.
_INLINE_EMPHASIS_WORD_RE = re.compile(
    r"(?<![\w*_])(?P<marker>\*\*|__|\*|_)(?P<word>[^\W_](?:[^\W_]|['\u2019-])*)(?P=marker)(?![\w*_])"
)
_MATCH_TEXT_PROTECTED_RE = re.compile(r"(`+)(?:(?!\1).)*\1|\S*/\S*", re.DOTALL)


def normalize_match_text(text: str) -> str:
    """Remove harmless single-word Markdown emphasis from assistant prose."""
    parts: list[str] = []
    cursor = 0
    for protected in _MATCH_TEXT_PROTECTED_RE.finditer(text):
        parts.extend(
            (
                _INLINE_EMPHASIS_WORD_RE.sub(r"\g<word>", text[cursor : protected.start()]),
                protected.group(),
            )
        )
        cursor = protected.end()
    parts.append(_INLINE_EMPHASIS_WORD_RE.sub(r"\g<word>", text[cursor:]))
    return "".join(parts)
