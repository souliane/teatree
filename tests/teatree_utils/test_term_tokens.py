"""Every token keeps the span of the characters it came from, in ASCII and beyond."""

import pytest

from teatree.utils.term_tokens import token_spans

DOTTED_I = chr(0x130)


@pytest.mark.parametrize(
    ("text", "spans"),
    [
        ("v2Owner", [("v2", 0, 2), ("owner", 2, 7)]),
        (f"{DOTTED_I} v2Owner", [("i", 0, 1), ("v2", 2, 4), ("owner", 4, 9)]),
        (f"{DOTTED_I} XMLParser", [("i", 0, 1), ("xml", 2, 5), ("parser", 5, 11)]),
    ],
)
def test_each_token_keeps_the_span_it_came_from(text: str, spans: list[tuple[str, int, int]]) -> None:
    assert token_spans(text) == spans
