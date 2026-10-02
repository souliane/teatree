"""Is this prompt a bare autonomous loop tick, or genuine user content?

One predicate, two consumers: the #58 live-presence heartbeat must not mistake a
cron-fired tick for a keystroke, and the #4195 operator-message recorder must not
fingerprint machine text as the operator's own words. Extracted from the
shrink-only ``hook_router`` so both read the same answer.

Stdlib-only at import (the two helpers it composes are stdlib-only siblings), so
it stays importable from the cold hook subprocess.
"""

from typing import Final

from hooks.scripts.loop_registrations import is_bare_loop_tick_prompt
from hooks.scripts.skill_loader_input import strip_ambient_context

LOOP_PROMPT: Final[str] = "Run `t3 loops tick` in Bash, then briefly report the tick summary."


def is_bare_loop_prompt(prompt: str) -> bool:
    """True when *prompt* is a PURE autonomous loop tick (no user content).

    A cron-fired tick reaches ``UserPromptSubmit`` as the loop prompt plus,
    optionally, the harness-injected ``<system-reminder>`` ambient blocks — both
    strip down to exactly the bare loop prompt. Two bare shapes count: the legacy
    fat-tick :data:`LOOP_PROMPT` and a per-loop tick ``t3 loops tick --loop
    <name>`` (#2650, recognised via the seam-synced
    :func:`is_bare_loop_tick_prompt`). A genuine fresh user prompt that the
    harness delivers PREFIXED by the loop continuation text leaves residual user
    content after the strip, so it is NOT bare. The ambient strip reuses
    :func:`strip_ambient_context` (the same normalisation the skill-load gate
    applies), keeping one definition of "what the harness appends".
    """
    stripped = strip_ambient_context(prompt)
    return stripped == LOOP_PROMPT.strip() or is_bare_loop_tick_prompt(stripped)
