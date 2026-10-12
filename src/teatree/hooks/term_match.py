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

The token rule itself lives in :mod:`teatree.utils.term_tokens`, which the
allow-list carve-out reads too, so the two can never split text differently.
A fully-lowercase glued spelling has no case boundary and stays one token
(``democorp``); the multi-token glued fallback in :func:`_contains_run`
restores coverage of THAT form for multi-word terms only, so a term
``demo-corp`` also matches the bare token ``democorp`` without loosening
single-word-term behaviour. (All examples are synthetic — real term values
live only in the operator's local config, never in this public source.)
"""

import functools

from teatree.utils.exact_carve_out import mask_exempt
from teatree.utils.term_tokens import token_spans, tokens


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


@functools.lru_cache(maxsize=4096)
def _term_run(term: str) -> tuple[str, ...]:
    return tuple(tokens(term))


def terms_in_reach(text: str, terms: tuple[str, ...]) -> tuple[str, ...]:
    """The *terms* some line of *text* could match: a superset that lets a whole-file scan skip a clean file."""
    present = set(tokens(text))
    return tuple(
        term for term in terms if (run := _term_run(term)) and (present.issuperset(run) or "".join(run) in present)
    )


def matched_term(text: str, terms: tuple[str, ...], allowlist: tuple[str, ...] = ()) -> str | None:
    """Return the first configured *term* whose tokens appear in *text*, else ``None``.

    A term matches when its own tokenization is a contiguous run of whole
    tokens in *text* (case-insensitive). Terms that tokenize to nothing
    (pure punctuation) never match.

    *allowlist* exempts each EXACT occurrence of a company-owned identifier
    (:func:`teatree.utils.exact_carve_out.mask_exempt`), so a shorter banned term
    never surfaces inside it, while an identifier that extends one is still scanned.
    """
    text_tokens = tokens(mask_exempt(text, allowlist))
    for term in terms:
        term_tokens = tokens(term)
        if term_tokens and _contains_run(text_tokens, term_tokens):
            return term
    return None


def line_matches(line: str, terms: tuple[str, ...], allowlist: tuple[str, ...] = ()) -> bool:
    """Whether any configured term's tokens appear as a whole-token run in *line*."""
    return matched_term(line, terms, allowlist) is not None


def iter_term_matches(text: str, term: str) -> list[tuple[str, int]]:
    """Every whole-token occurrence of *term* in *text* as ``(matched, position)``.

    A single-token *term* yields one entry per matching token; a multi-token
    *term* yields one entry per contiguous run (the glued-token fallback is
    not position-tracked — a glued single token still yields its own span).
    *position* is the offset in *text* where the match starts.
    """
    term_tokens = tokens(term)
    if not term_tokens:
        return []
    spans = token_spans(text)
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


def file_matches(
    path: str,
    terms: tuple[str, ...],
    *,
    allowlist: tuple[str, ...] = (),
) -> list[tuple[int, str, str]]:
    """Scan a file line-by-line and return every banned-term hit.

    Each hit is ``(line_number, matched_term, line)``. *allowlist* carves out
    the company's own identifiers per line (:func:`matched_term`). This is the
    SINGLE file-scanning path that ``scripts/hooks/check-banned-terms.sh``
    shells out to, so the shell hook and the in-process gates share one matcher
    implementation and cannot drift apart.
    """
    from pathlib import Path  # noqa: PLC0415 -- keep the module import-light for hot-path callers

    hits: list[tuple[int, str, str]] = []
    if not terms:
        return hits
    text = Path(path).read_text(encoding="utf-8")
    for line_number, line in enumerate(text.splitlines(), start=1):
        term = matched_term(line, terms, allowlist)
        if term is not None:
            hits.append((line_number, term, line))
    return hits
