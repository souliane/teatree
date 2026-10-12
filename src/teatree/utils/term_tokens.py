"""The one token rule the term matcher and its allow-list carve-out both read.

A token is a run of ASCII letters and digits in the lowercased text, also split at a camelCase boundary
(``acmeProduct``) and before the capital that starts a word after an acronym (``XMLParser``).
"""

import bisect
import re

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_CASE_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_TOKEN_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789")

type TokenSpan = tuple[str, int, int]


def tokens(text: str) -> list[str]:
    lowered = text.lower()
    if len(lowered) == len(text) and not _CASE_BOUNDARY_RE.search(text):
        return _TOKEN_RE.findall(lowered)
    return [token for token, _, _ in token_spans(text)]


def token_spans(text: str) -> list[TokenSpan]:
    """Each token of *text* with the ``(start, end)`` of the characters it came from in *text*."""
    lowered = text.lower()
    if len(lowered) != len(text):
        return _spans_lowering_each_character(text)
    cuts = [cut.start() for cut in _CASE_BOUNDARY_RE.finditer(text)]
    spans: list[TokenSpan] = []
    for run in _TOKEN_RE.finditer(lowered):
        start, end = run.span()
        index = bisect.bisect_right(cuts, start)
        while index < len(cuts) and cuts[index] < end:
            spans.append((lowered[start : cuts[index]], start, cuts[index]))
            start = cuts[index]
            index += 1
        spans.append((lowered[start:end], start, end))
    return spans


def _spans_lowering_each_character(text: str) -> list[TokenSpan]:
    # U+0130 lowercases to two characters, so lowered positions stop matching the text's.
    cuts = {cut.start() for cut in _CASE_BOUNDARY_RE.finditer(text)}
    spans: list[TokenSpan] = []
    token, start, end = "", 0, 0
    for index, char in enumerate(text):
        if index in cuts and token:
            spans.append((token, start, end))
            token = ""
        for lowered in char.lower():
            if lowered in _TOKEN_CHARS:
                start = start if token else index
                token += lowered
                end = index + 1
            elif token:
                spans.append((token, start, end))
                token = ""
    if token:
        spans.append((token, start, end))
    return spans
