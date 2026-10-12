"""Every token keeps the span of the characters it came from, in ASCII and beyond."""

from teatree.utils.term_tokens import token_spans

DOTTED_I = chr(0x130)


def test_each_token_keeps_the_span_it_came_from() -> None:
    assert token_spans(f"{DOTTED_I} v2Owner") == [("i", 0, 1), ("v2", 2, 4), ("owner", 4, 9)]
