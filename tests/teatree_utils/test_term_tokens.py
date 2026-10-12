"""Every token keeps the span of the characters it came from, in ASCII and beyond."""

import pytest

from teatree.utils.term_tokens import token_spans

DOTTED_I = chr(0x130)


@pytest.mark.parametrize(
    ("text", "spans"),
    [
        ("v2Owner", [("v2", 0, 2), ("owner", 2, 7)]),
        (f"{DOTTED_I} AcmeOwner", [("i", 0, 1), ("acme", 2, 6), ("owner", 6, 11)]),
    ],
)
def test_each_token_keeps_the_span_it_came_from(text: str, spans: list[tuple[str, int, int]]) -> None:
    assert token_spans(text) == spans
