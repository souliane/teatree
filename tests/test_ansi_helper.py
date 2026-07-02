"""Tests for the shared ANSI-stripping helper.

The contract that matters (souliane/teatree#2359): :func:`strip_ansi` removes
Rich/Click SGR escape codes so a substring/regex assertion against captured
CLI output is deterministic regardless of whether color was forced.
"""

from tests._ansi import strip_ansi


class TestStripAnsi:
    def test_removes_sgr_codes(self) -> None:
        assert strip_ansi("\x1b[1mC901\x1b[0m") == "C901"

    def test_removes_codes_mid_token(self) -> None:
        assert strip_ansi("TODO-\x1b[1;36m7\x1b[0m") == "TODO-7"

    def test_plain_text_is_unchanged(self) -> None:
        assert strip_ansi("plain text, no codes") == "plain text, no codes"

    def test_multiple_codes_in_one_string(self) -> None:
        colored = "\x1b[1msrc/x.py\x1b[0m\x1b[36m:\x1b[0m1\x1b[36m:\x1b[0m5\x1b[36m:\x1b[0m error[C901]"
        assert strip_ansi(colored) == "src/x.py:1:5: error[C901]"
