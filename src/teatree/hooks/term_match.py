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
term are tokenized, with ``-``, ``_``, whitespace, punctuation AND camelCase
boundaries all acting as token separators, and matching is case-insensitive.
A term matches iff its token list appears as a CONTIGUOUS run of whole tokens
in the text's token list: a single-token term reduces to set membership; a
multi-token term is a contiguous-sublist check (with a glued-token fallback —
see below).

Concretely, a term ``acme`` matches the standalone token in ``acme``,
``xx-acme-zz``, ``acme-corp``, ``Acme,`` and ``acmeProduct``/``AcmeProduct``
(camelCase splits to ``[acme, product]``) but NOT ``acmecorp``,
``acmeology`` or ``pacme`` (one unbroken lowercase run). A term ``acme-corp``
(tokens ``[acme, corp]``) matches a contiguous ``[acme, corp]`` run AND the
glued single token ``acmecorp``. ``home-base`` and ``home base`` both
tokenize to ``[home, base]`` and so match each other; an underscore term
such as ``widget_count`` tokenizes to ``[widget, count]`` and matches both
``widget_count`` and ``widget count``.

Tokenizing with the standard-library ``re`` is deliberate: ``re.findall``
over an alphanumeric character class IS the standard tool for this, so no
third-party word-list / profanity dependency is warranted.

CamelCase is split BEFORE lowercasing by inserting a separator at
``[a-z0-9]→[A-Z]`` transitions (``acmeProduct`` → ``acme Product``) and at
acronym ``[A-Z]+→[A-Z][a-z]`` transitions (``XMLParser`` → ``XML Parser``),
so a glued identifier no longer hides a term. A clean identifier with no
embedded term (``getUserName`` → ``[get, user, name]``) is unaffected. A
fully-lowercase glued spelling has no case boundary and stays one token
(``democorp``); the multi-token glued fallback in :func:`_contains_run`
restores coverage of THAT form for multi-word terms only, so a term
``demo-corp`` also matches the bare token ``democorp`` without loosening
single-word-term behaviour. (All examples are synthetic — real term values
live only in the operator's local config, never in this public source.)
"""

import re

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_CAMEL_BOUNDARY_RE = re.compile(r"([a-z0-9])([A-Z])")
_ACRONYM_BOUNDARY_RE = re.compile(r"([A-Z]+)([A-Z][a-z])")


def tokens(text: str) -> list[str]:
    """Split *text* into lowercase alphanumeric tokens.

    Every non-alphanumeric character (``-``, ``_``, whitespace, punctuation)
    AND every camelCase/PascalCase boundary is a separator, so
    ``"xx-acme, zz"`` → ``["xx", "acme", "zz"]``, ``"widget_count"`` →
    ``["widget", "count"]`` and ``"acmeProduct"`` → ``["acme", "product"]``.
    """
    split = _ACRONYM_BOUNDARY_RE.sub(r"\1 \2", text)
    split = _CAMEL_BOUNDARY_RE.sub(r"\1 \2", split)
    return _TOKEN_RE.findall(split.lower())


def _contains_run(haystack: list[str], needle: list[str]) -> bool:
    """Whether *needle* appears as a contiguous sublist of *haystack*.

    A multi-token *needle* also matches a single *haystack* token equal to
    its tokens glued with no separator, so a fully-lowercase glued spelling
    (``democorp`` for term ``demo-corp``) is caught even though it has no
    camelCase boundary to split on.
    """
    if not needle:
        return False
    if len(needle) == 1:
        return needle[0] in haystack
    first = needle[0]
    span = len(needle)
    if any(token == first and haystack[start : start + span] == needle for start, token in enumerate(haystack)):
        return True
    return "".join(needle) in haystack


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
