"""Every token keeps the span of the characters it came from, in ASCII and beyond."""

import pytest

from teatree.utils.term_tokens import token_spans, tokens

DOTTED_I = chr(0x130)
KELVIN = chr(0x212A)

SPANS = [
    ("xx-acme, zz_qq", [("xx", 0, 2), ("acme", 3, 7), ("zz", 9, 11), ("qq", 12, 14)]),
    ("acmeProduct", [("acme", 0, 4), ("product", 4, 11)]),
    ("XMLParser", [("xml", 0, 3), ("parser", 3, 9)]),
    ("v2Owner", [("v2", 0, 2), ("owner", 2, 7)]),
    ("--- , .", []),
    (f"{DOTTED_I}acme", [("i", 0, 1), ("acme", 1, 5)]),
    (f"{DOTTED_I}AcmeOwner", [("i", 0, 1), ("acme", 1, 5), ("owner", 5, 10)]),
    (f"{KELVIN}cme", [("kcme", 0, 4)]),
]


@pytest.mark.parametrize(("text", "spans"), SPANS)
def test_each_token_keeps_the_span_it_came_from(text: str, spans: list[tuple[str, int, int]]) -> None:
    assert token_spans(text) == spans


@pytest.mark.parametrize(("text", "spans"), SPANS)
def test_tokens_are_the_spans_without_positions(text: str, spans: list[tuple[str, int, int]]) -> None:
    assert tokens(text) == [token for token, _, _ in spans]
