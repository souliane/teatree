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

_COLOR_FORCING_VARS = ("FORCE_COLOR", "CLICOLOR_FORCE", "CLICOLORS")


def no_color_env() -> dict[str, str]:
    """Return a copy of the process env with color-forcing vars neutralized.

    Pass as ``subprocess.run(..., env=no_color_env())`` for any subprocess
    whose stdout/stderr a test parses as plain text.
    """
    env = {k: v for k, v in os.environ.items() if k not in _COLOR_FORCING_VARS}
    env["NO_COLOR"] = "1"
    return env
