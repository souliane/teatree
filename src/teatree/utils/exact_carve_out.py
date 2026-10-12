"""Exempt an allow-listed identifier only where it occurs exactly, never inside a longer identifier.

An entry is a run of the matcher's tokens (:mod:`teatree.utils.term_tokens`), with ``-``/``_``/space/``.``
interchangeable between tokens. An occurrence is extended, so not exempt, when a letter, digit, ``-`` or ``_``
touches either end; for an entry holding ``.`` or ``@`` also when a ``.``, ``@`` or ``+`` joins either end to a
letter or digit.
"""

import functools
import itertools
from dataclasses import dataclass

from teatree.utils.term_tokens import TokenSpan, token_spans

_JOINING_SEPARATORS = frozenset({"", "-", "_", " ", "."})
_WORD_JOINERS = frozenset("-_")
_ADDRESS_JOINERS = frozenset(".@+")


@dataclass(frozen=True, slots=True)
class _Entry:
    tokens: tuple[str, ...]
    separators: tuple[str, ...]
    address_like: bool


@functools.cache
def _entry(raw: str) -> _Entry:
    tokens = token_spans(raw)
    return _Entry(
        tokens=tuple(token for token, _, _ in tokens),
        separators=tuple(raw[left[2] : right[1]] for left, right in itertools.pairwise(tokens)),
        address_like="." in raw or "@" in raw,
    )


def _same_separator(found: str, expected: str) -> bool:
    return found == expected or (found in _JOINING_SEPARATORS and expected in _JOINING_SEPARATORS)


def _extended(text: str, start: int, end: int, *, address_like: bool) -> bool:
    before = text[start - 1] if start else ""
    after = text[end] if end < len(text) else ""
    if before.isalnum() or after.isalnum() or before in _WORD_JOINERS or after in _WORD_JOINERS:
        return True
    if not address_like:
        return False
    return (before in _ADDRESS_JOINERS and start > 1 and text[start - 2].isalnum()) or (
        after in _ADDRESS_JOINERS and end + 1 < len(text) and text[end + 1].isalnum()
    )


def _occurrences(text: str, tokens: list[TokenSpan], entry: _Entry) -> list[tuple[int, int]]:
    width = len(entry.tokens)
    spans: list[tuple[int, int]] = []
    for index in range(len(tokens) - width + 1):
        window = tokens[index : index + width]
        if tuple(token for token, _, _ in window) != entry.tokens:
            continue
        found = (text[left[2] : right[1]] for left, right in itertools.pairwise(window))
        if all(itertools.starmap(_same_separator, zip(found, entry.separators, strict=True))):
            spans.append((window[0][1], window[-1][2]))
    return spans


def mask_exempt(text: str, allowlist: tuple[str, ...]) -> str:
    """*text* with every exact allow-entry occurrence blanked to spaces, so positions are kept."""
    lowered = text.lower()
    entries = [entry for entry in map(_entry, allowlist) if entry.tokens and entry.tokens[0] in lowered]
    if not entries:
        return text
    tokens = token_spans(text)
    masked = list(text)
    for entry in entries:
        for start, end in _occurrences(text, tokens, entry):
            if not _extended(text, start, end, address_like=entry.address_like):
                masked[start:end] = " " * (end - start)
    return "".join(masked)
