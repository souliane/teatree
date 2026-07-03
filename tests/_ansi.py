r"""Shared test-infra helper: strip ANSI SGR codes from captured CLI output (souliane/teatree#2359).

Rich colorizes when color is on (a real TTY, or a leaked ``FORCE_COLOR``),
wrapping tokens mid-string (``TODO-\\x1b[1;36m7\\x1b[0m``), so a literal
substring/regex assert against raw captured output breaks under color but
passes when piped. Assert on de-colorized text so the check is deterministic
in either color state.
"""

import re

_ANSI_SGR = re.compile(r"\x1b\[[0-9;]*m")


def strip_ansi(text: str) -> str:
    return _ANSI_SGR.sub("", text)
