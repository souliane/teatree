"""Smart-quote normalisation shared by the publish-leak gates.

Both quote-shaped gates — the #1213 quote-scanner and the #4195 verbatim
operator-paste gate — decide from ASCII-quote patterns, so a curly-quoted body
would bypass every one of them unless the input is normalised first. One table,
one function, two consumers (#81): a new typographic variant is taught here once
rather than per gate. Stdlib-only so it stays importable from the cold
PreToolUse subprocess.
"""

from typing import Final

# Code points referenced by ``\N{...}`` so the lint checker is not confused by
# ambiguous glyphs in the source file.
SMART_QUOTE_TRANSLATIONS: Final[dict[int, str]] = {
    # Double quotes
    ord("\N{LEFT DOUBLE QUOTATION MARK}"): '"',
    ord("\N{RIGHT DOUBLE QUOTATION MARK}"): '"',
    ord("\N{DOUBLE LOW-9 QUOTATION MARK}"): '"',
    ord("\N{DOUBLE HIGH-REVERSED-9 QUOTATION MARK}"): '"',
    ord("\N{LEFT-POINTING DOUBLE ANGLE QUOTATION MARK}"): '"',
    ord("\N{RIGHT-POINTING DOUBLE ANGLE QUOTATION MARK}"): '"',
    # Single quotes / apostrophes
    ord("\N{LEFT SINGLE QUOTATION MARK}"): "'",
    ord("\N{RIGHT SINGLE QUOTATION MARK}"): "'",
    ord("\N{SINGLE LOW-9 QUOTATION MARK}"): "'",
    ord("\N{SINGLE HIGH-REVERSED-9 QUOTATION MARK}"): "'",
}


def normalize_quotes(text: str) -> str:
    """Translate Unicode smart-quote variants to straight ASCII quotes.

    The detection regexes are written against ASCII quotes; normalising upstream
    means a single regex per shape continues to cover every typographic variant
    a publish surface might emit.
    """
    return text.translate(SMART_QUOTE_TRANSLATIONS)
