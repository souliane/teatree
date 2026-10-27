"""ANSI-C quoted shell text decoding."""

import string
from typing import Final

_ANSI_C_SIMPLE_ESCAPES: Final[dict[str, str]] = {
    "a": "\a",
    "b": "\b",
    "e": "\x1b",
    "E": "\x1b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "v": "\v",
    "\\": "\\",
    "'": "'",
    '"': '"',
    "?": "?",
}


def decode_ansi_c_digits(literal: str, start: int, max_len: int, alphabet: str) -> str:
    end = start
    limit = min(start + max_len, len(literal))
    while end < limit and literal[end] in alphabet:
        end += 1
    return literal[start:end]


def decode_ansi_c_hex(literal: str, start: int, max_len: int) -> tuple[str, int]:
    digits = decode_ansi_c_digits(literal, start, max_len, string.hexdigits)
    if not digits:
        return literal[start - 1], start
    try:
        return chr(int(digits, 16)), start + len(digits)
    except ValueError:
        return literal[start - 1], start + len(digits)


def decode_ansi_c_octal(literal: str, start: int) -> tuple[str, int]:
    digits = decode_ansi_c_digits(literal, start, 3, string.octdigits)
    return chr(int(digits, 8) & 0xFF), start + len(digits)


_ANSI_C_HEX_WIDTH: Final[dict[str, int]] = {"x": 2, "u": 4, "U": 8}


def decode_ansi_c_escape(literal: str, i: int) -> tuple[str, int]:
    if i + 1 >= len(literal):
        return literal[i], i + 1
    nxt = literal[i + 1]
    if nxt in _ANSI_C_SIMPLE_ESCAPES:
        return _ANSI_C_SIMPLE_ESCAPES[nxt], i + 2
    hex_width = _ANSI_C_HEX_WIDTH.get(nxt)
    if hex_width is not None:
        return decode_ansi_c_hex(literal, i + 2, hex_width)
    if nxt in string.octdigits:
        return decode_ansi_c_octal(literal, i + 1)
    if nxt == "c" and i + 2 < len(literal):
        return chr(ord(literal[i + 2].upper()) ^ 0x40), i + 3
    return literal[i : i + 2], i + 2


def decode_ansi_c(literal: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(literal):
        if literal[i] != "\\":
            out.append(literal[i])
            i += 1
            continue
        decoded, i = decode_ansi_c_escape(literal, i)
        out.append(decoded)
    return "".join(out).partition("\x00")[0]
