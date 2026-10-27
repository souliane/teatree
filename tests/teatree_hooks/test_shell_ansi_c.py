"""Preserve ANSI-C shell quoting when the lexer decoder moves modules."""

import pytest

from teatree.hooks._shell_ansi_c import decode_ansi_c
from teatree.hooks._shell_lexer import tokenize


@pytest.mark.parametrize(
    ("literal", "expected"),
    [
        (r"\n", "\n"),
        (r"\'", "'"),
        (r"\x41\u0042\U00000043", "ABC"),
        (r"\101", "A"),
        (r"\cA", "\x01"),
        (r"\q", r"\q"),
        (r"\xZ", "xZ"),
        (r"\UFFFFFFFF", "U"),
        (r"a\0b", "a"),
    ],
)
def test_ansi_c_decoder_matches_shell_token(literal: str, expected: str) -> None:
    assert decode_ansi_c(literal) == expected
    assert tokenize(f"printf $'{literal}'")[1].value == expected


def test_terminal_backslash_is_preserved_by_decoder() -> None:
    assert decode_ansi_c("abc\\") == "abc\\"
