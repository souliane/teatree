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

# Email carve-out: a term that appears ONLY inside an author/contact email
# address (``adrien.cossa@internalterm.example``) is not a leak — the address
# is the author's identity, not a customer reference. Emails are blanked
# BEFORE tokenizing so the term inside one never reaches the matcher. This is
# the SINGLE definition of the carve-out; both the in-process gates and the
# ``check-banned-terms.sh`` shell hook (which shells out to :func:`file_matches`)
# share it, so the two paths cannot drift apart.
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


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


def _token_spans(text: str) -> list[tuple[str, int, int]]:
    """Tokenize *text* keeping each token's ``(token, start, end)`` span.

    CamelCase/acronym boundaries are split the same way :func:`tokens` does
    (a separator is inserted at each boundary), so the returned offsets are
    into the camelCase-SPLIT text, not the original. Callers use the offset
    only as an informational position, so the split-text offset is fine.
    """
    split = _ACRONYM_BOUNDARY_RE.sub(r"\1 \2", text)
    split = _CAMEL_BOUNDARY_RE.sub(r"\1 \2", split).lower()
    return [(m.group(0), m.start(), m.end()) for m in _TOKEN_RE.finditer(split)]


def iter_term_matches(text: str, term: str) -> list[tuple[str, int]]:
    """Every whole-token occurrence of *term* in *text* as ``(matched, position)``.

    A single-token *term* yields one entry per matching token; a multi-token
    *term* yields one entry per contiguous run (the glued-token fallback is
    not position-tracked — a glued single token still yields its own span).
    *position* is an informational offset into the camelCase-split text.
    """
    term_tokens = tokens(term)
    if not term_tokens:
        return []
    spans = _token_spans(text)
    matches: list[tuple[str, int]] = []
    span = len(term_tokens)
    for i in range(len(spans) - span + 1):
        window = spans[i : i + span]
        if [w[0] for w in window] == term_tokens:
            matched = "".join(w[0] for w in window)
            matches.append((matched, window[0][1]))
    if span > 1:
        glued = "".join(term_tokens)
        matches.extend((tok, start) for tok, start, _ in spans if tok == glued)
    return matches


def strip_emails(text: str) -> str:
    """Blank every email address in *text* (the author/contact email carve-out).

    A term that appears only inside an author or contact email address is the
    author's own identity, not a customer reference, so emails are replaced by
    a single space before matching.
    """
    return _EMAIL_RE.sub(" ", text)


def file_matches(path: str, terms: tuple[str, ...], *, carve_out_emails: bool = True) -> list[tuple[int, str, str]]:
    """Scan a file line-by-line and return every banned-term hit.

    Each hit is ``(line_number, matched_term, line)``. The email carve-out
    (:func:`strip_emails`) is applied per line before matching when
    *carve_out_emails* is true. This is the SINGLE file-scanning path that
    ``scripts/hooks/check-banned-terms.sh`` shells out to, so the shell hook
    and the in-process gates share one matcher implementation and cannot
    drift apart.
    """
    from pathlib import Path  # noqa: PLC0415 -- keep the module import-light for hot-path callers

    hits: list[tuple[int, str, str]] = []
    if not terms:
        return hits
    text = Path(path).read_text(encoding="utf-8")
    for line_number, line in enumerate(text.splitlines(), start=1):
        candidate = strip_emails(line) if carve_out_emails else line
        term = matched_term(candidate, terms)
        if term is not None:
            hits.append((line_number, term, line))
    return hits
