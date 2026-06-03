r"""Whole-token matcher shared by both configured term-list gates.

Both the ``[teatree].banned_terms`` posting gate (#1415) and the
``[overlay_leak].terms`` core-leak gate (BLUEPRINT § 1) need to answer the
same question: does a configured term appear in a piece of text? The old
answer was a ``\b(term)\b`` regex, which (because ``\b`` only marks the
boundary between a word and a non-word character) still let a SHORT term
match inside a longer run of the same alphabet — e.g. a short term would
surface inside a longer legitimate word once the matcher was loosened,
producing false-positive blocks. (All examples below use neutral synthetic
terms; the real configured term values live only in the operator's local
config, never in this public source.)

This module replaces that with WHOLE-TOKEN matching. Both the text and the
term are tokenized on any non-alphanumeric character
(``re.findall(r"[a-z0-9]+", s.lower())``), so ``-``, ``_``, whitespace, and
punctuation are all token separators and matching is case-insensitive. A
term matches iff its token list appears as a CONTIGUOUS run of whole tokens
in the text's token list: a single-token term reduces to set membership; a
multi-token term is a contiguous-sublist check.

Concretely, a term ``acme`` matches the standalone token in ``acme``,
``xx-acme-zz``, ``acme-corp`` and ``Acme,`` but NOT ``acmecorp``,
``acmeology`` or ``pacme``. A term ``acme-corp`` (tokens ``[acme, corp]``)
matches a contiguous ``[acme, corp]`` run. ``home-base`` and ``home base``
both tokenize to ``[home, base]`` and so match each other; an underscore
term such as ``widget_count`` tokenizes to ``[widget, count]`` and matches
both ``widget_count`` and ``widget count``.

Tokenizing with the standard-library ``re`` is deliberate: ``re.findall``
over an alphanumeric character class IS the standard tool for this, so no
third-party word-list / profanity dependency is warranted.

TRADE-OFF (intentional, per the gate owner's directive): whole-token
matching removes the substring false positives, but it also means a term
that is only a PREFIX (or any embedded fragment) of one unbroken word no
longer matches — e.g. a term ``demo-base`` no longer matches the glued
camelCase identifier ``demoBase`` (which tokenizes to the single token
``demobase``). A term must appear as its own whole token(s), separated by a
non-alphanumeric character, to match.
"""

import re

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def tokens(text: str) -> list[str]:
    """Split *text* into lowercase alphanumeric tokens.

    Every non-alphanumeric character (``-``, ``_``, whitespace, punctuation)
    is a separator, so ``"xx-acme, zz"`` → ``["xx", "acme", "zz"]`` and
    ``"widget_count"`` → ``["widget", "count"]``.
    """
    return _TOKEN_RE.findall(text.lower())


def _contains_run(haystack: list[str], needle: list[str]) -> bool:
    """Whether *needle* appears as a contiguous sublist of *haystack*."""
    if not needle:
        return False
    if len(needle) == 1:
        return needle[0] in haystack
    first = needle[0]
    span = len(needle)
    return any(token == first and haystack[start : start + span] == needle for start, token in enumerate(haystack))


def matched_term(text: str, terms: tuple[str, ...]) -> str | None:
    """Return the first configured *term* whose tokens appear in *text*, else ``None``.

    A term matches when its own tokenization is a contiguous run of whole
    tokens in *text* (case-insensitive). Terms that tokenize to nothing
    (pure punctuation) never match.
    """
    text_tokens = tokens(text)
    for term in terms:
        term_tokens = tokens(term)
        if term_tokens and _contains_run(text_tokens, term_tokens):
            return term
    return None


def line_matches(line: str, terms: tuple[str, ...]) -> bool:
    """Whether any configured term's tokens appear as a whole-token run in *line*."""
    return matched_term(line, terms) is not None
