"""Is this prompt a bare autonomous loop tick, or genuine user content?

Stdlib-only at import, so it stays importable from the cold hook subprocess.
"""

import re
from typing import Final

from hooks.scripts.skill_loader_input import strip_ambient_context

LOOP_PROMPT: Final[str] = "Run `t3 loops tick` in Bash, then briefly report the tick summary."

# Kept in sync with the worker's subprocess-tick argv and the manual ``t3 loops tick --loop <name>``.
_RUN_CMD_RE: Final[re.Pattern[str]] = re.compile(r"t3 loops tick --loop (?P<name>[^\s`]+)")
_BARE_TICK_RE: Final[re.Pattern[str]] = re.compile(
    r"^Run `t3 loops tick --loop \S+` in Bash, then briefly report the tick summary\.$"
)


def loop_name_from_prompt(prompt: str) -> str | None:
    """The ``--loop <name>`` a per-loop tick prompt runs, or ``None`` when it is not one."""
    match = _RUN_CMD_RE.search(prompt)
    return match.group("name") if match else None


def is_bare_loop_prompt(prompt: str) -> bool:
    """True when *prompt* is a PURE autonomous loop tick, once the harness's ambient blocks are stripped.

    A genuine user prompt the harness delivers PREFIXED by the loop continuation text
    keeps residual user content after the strip, so it is not bare.
    """
    stripped = strip_ambient_context(prompt)
    return stripped == LOOP_PROMPT.strip() or bool(_BARE_TICK_RE.match(stripped))
