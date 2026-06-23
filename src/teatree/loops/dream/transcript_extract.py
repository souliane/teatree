"""Decide which transcript lines survive into the dream distiller input (#1933).

A raw session/sub-agent/task transcript is mostly chatter the LLM distiller must
never see. Two complementary keepers run per line. The keyword gate
(:data:`TRANSCRIPT_SIGNALS`) keeps gate BLOCKs, deny streaks, and
``feedback_``/``BINDING``/retro/cold-review markers — necessary but NOT
sufficient, because the highest-signal drift evidence is the user's own
correction PROSE, which carries none of those tokens. :func:`looks_like_user_correction`
is the keyword-blind keeper for that prose: a raw user-correction / frustration
turn (imperative-negation / anger cues, a ``why … ?`` question, repeated ``!``),
plus repeated near-identical user turns.

Without the second keeper the keyword gate filtered fresh user-correction prose
out before the distiller ever saw it — the gap this module closes.
"""

import re
from collections import Counter
from collections.abc import Sequence

#: Transcript lines worth keeping on keyword alone — the rest is chatter that
#: must never reach the LLM prompt. Necessary-not-sufficient: a line also
#: survives when it reads like a raw user-correction turn (see
#: :func:`looks_like_user_correction`), which carries none of these.
TRANSCRIPT_SIGNALS = (
    "TEATREE GATE",
    "BLOCK",
    "DENIED",
    "feedback_",
    "BINDING",
    "retro",
    "user-correction",
    "cold review",
)

#: Frustration / imperative-negation cues that mark a raw user-correction turn
#: carrying no keyword signal — the prose the keyword gate alone filters out.
_CORRECTION_CUES = (
    "again",
    "told you",
    "stop",
    "do not",
    "don't",
    "not ",
    "never",
)

_USER_TURN_RE = re.compile(r'"(?:type|role)"\s*:\s*"user"')
_WHY_QUESTION_RE = re.compile(r"\bwhy\b[^?]*\?")

#: A cheap substring pre-gate: only a line that even mentions ``"user"`` can be a
#: user turn, so the costlier :data:`_USER_TURN_RE` regex (and the per-cue scan)
#: never run against the assistant-line bulk of a transcript.
_USER_TURN_HINT = '"user"'

#: A user turn must recur at least this many times within one transcript to count
#: as a repeated near-identical correction independent of the cue list.
_REPEAT_THRESHOLD = 2


def looks_like_user_correction(line: str) -> bool:
    """True when *line* reads like a raw user-correction / frustration turn.

    Keyword-blind by design: the highest-signal drift evidence is the user's own
    prose ("I told you again", "do not build a new", "stop", "why … ?", "no!!"),
    which carries none of :data:`TRANSCRIPT_SIGNALS`. Only a USER turn qualifies —
    the agent's own text echoing a cue is not a correction OF the agent. A bare
    cue inside an assistant line is ignored.
    """
    if _USER_TURN_HINT not in line or not _USER_TURN_RE.search(line):
        return False
    lowered = line.lower()
    if any(cue in lowered for cue in _CORRECTION_CUES):
        return True
    if _WHY_QUESTION_RE.search(lowered):
        return True
    return "!!" in line


def _repeated_user_turns(lines: Sequence[str]) -> set[str]:
    user_lines = [line for line in lines if _USER_TURN_HINT in line and _USER_TURN_RE.search(line)]
    counts = Counter(line.strip() for line in user_lines if line.strip())
    return {line for line, count in counts.items() if count > _REPEAT_THRESHOLD}


def high_signal_lines(raw: str) -> str:
    """Keep only the lines worth distilling: keyword signals OR correction prose."""
    lines = raw.splitlines()
    repeated = _repeated_user_turns(lines)
    kept = [
        line
        for line in lines
        if any(signal in line for signal in TRANSCRIPT_SIGNALS)
        or looks_like_user_correction(line)
        or line.strip() in repeated
    ]
    return "\n".join(kept)


__all__ = ["TRANSCRIPT_SIGNALS", "high_signal_lines", "looks_like_user_correction"]
