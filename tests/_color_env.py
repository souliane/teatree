r"""Shared test-infra helper: a subprocess env hermetic against ambient color forcing (souliane/teatree#2359).

A dev shell that sets ``FORCE_COLOR``/``CLICOLOR_FORCE`` (common iTerm/oh-my-zsh
configs do) makes a subprocess CLI (``ruff``, ``t3``, ...) emit ANSI SGR codes
even when its stdout is piped — not a TTY. A test that regex/substring-matches
that output assumes plain text; a leaked ``FORCE_COLOR`` breaks the match
(``\\bC901\\b`` cannot straddle the ANSI ``\\x1b[1m`` sequence's trailing
``m``), giving an environment-dependent false failure that reproduces locally
but not in a clean CI container. ``NO_COLOR`` alone does not fix this — ruff
(and other tools) honour ``FORCE_COLOR`` ahead of ``NO_COLOR``, so the forcing
vars must be removed outright, not merely countermanded.
"""

import os
import re

_COLOR_FORCING_VARS = ("FORCE_COLOR", "CLICOLOR_FORCE", "CLICOLORS")


def no_color_env() -> dict[str, str]:
    """Return a copy of the process env with color-forcing vars neutralized.

    Pass as ``subprocess.run(..., env=no_color_env())`` for any subprocess
    whose stdout/stderr a test parses as plain text.
    """
    env = {k: v for k, v in os.environ.items() if k not in _COLOR_FORCING_VARS}
    env["NO_COLOR"] = "1"
    return env


#: Matches an ANSI SGR / CSI escape sequence.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def strip_ansi(text: str) -> str:
    r"""Return *text* with every ANSI escape sequence removed.

    The in-process counterpart of :func:`no_color_env`. A ``typer.testing``
    ``CliRunner`` renders help through rich, which styles an option name as
    SEPARATE spans (``--report`` becomes ``\x1b[1;36m-\x1b[0m\x1b[1;36m-report\x1b[0m``),
    so a substring match for ``"--report"`` fails on output that visibly
    contains it. ``NO_COLOR`` does not suppress this; stripping the codes does,
    and it stays correct however rich chooses to style.
    """
    return _ANSI_RE.sub("", text)
