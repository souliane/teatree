"""Decide which transcript lines survive into the dream distiller input (#1933).

A raw session/sub-agent/task transcript is mostly chatter the LLM distiller must
never see. Three complementary keepers run per line. The keyword gate
(:data:`TRANSCRIPT_SIGNALS`) keeps gate BLOCKs, deny streaks, and
``feedback_``/``BINDING``/retro/cold-review markers — necessary but NOT
sufficient, because the highest-signal drift evidence is the user's own
correction PROSE, which carries none of those tokens. :func:`looks_like_user_correction`
is the keyword-blind keeper for that prose: a raw user-correction / frustration
turn (imperative-negation / anger cues, a ``why … ?`` question, repeated ``!``),
plus repeated near-identical user turns.

The third keeper, :func:`looks_like_user_ask`, is the structural sibling of the
correction keeper for the "improve-with-new-stuff" half of dreaming (#2663): a USER
turn that reads like a manual directive/request t3 could automate away (imperative
cues "can you"/"please"/"let's", or operational-ACTION cues "hotfix"/"asap"/
"rollback"). Bare incident-state words ("production"/"broken"/"blocker"/"wedged")
are excluded (#2732) — they describe a situation, not a request. Clustered over many
nights, a recurring ask becomes an automatable-ask gap promoted to a fix under the
standing umbrella.

Without these keepers the keyword gate filtered fresh user-correction and user-ask
prose out before the distiller ever saw it — the gap this module closes.
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

#: Imperative-request and operational-urgency cues that mark a raw USER turn as a
#: directive/ask the agent could be taken out of the loop on — the keyword-blind
#: sibling of :data:`_CORRECTION_CUES`. The imperative cues read like a request
#: ("can you", "please", "let's"); the operational cues name an urgent ACTION the
#: user is asking for ("hotfix", "asap", "rollback") whose manual handling is exactly
#: what a new automation (e.g. a hotfix lane) would absorb. Bare INCIDENT-STATE words
#: ("production", "broken", "blocker", "wedged") are deliberately EXCLUDED (#2732):
#: they describe a situation, not a request, so they over-matched on incident chatter
#: that carried no user ask.
_ASK_CUES = (
    "can you",
    "could you",
    "please",
    "i need you to",
    "i want you to",
    "let's",
    "we should",
    "set up",
    "make sure",
    "go ahead and",
    "hotfix",
    "urgent",
    "asap",
    "drop everything",
    "rollback",
)

#: Declarative INSIGHT / root-cause / decision markers of a SUBSTANTIVE learning
#: line — the keyword-blind (of :data:`TRANSCRIPT_SIGNALS`) keeper for the rich raw
#: drift the literal-signal gate dropped. The correction/ask keepers cover the USER's
#: own prose, but the day's richest drift is often a DECLARATIVE finding the agent
#: recorded ("root caused the crash to a missing tenant filter", "turns out the
#: migration never applied", "decided to split the resolver"). Without this keeper the
#: keyword gate filtered that out before the distiller ever saw it, so a plain pass
#: distilled 0 clusters from a corpus full of real learnings (#2986). Unlike the
#: correction/ask keepers this is ROLE-AGNOSTIC — the literal-signal keeper already is,
#: and a lesson is a lesson whether the user stated it or the agent found it. The cues
#: stay tight to genuine finding/decision vocabulary, so mechanical status chatter
#: ("computed result row 7") and cue-free filler carry none of it and stay dropped.
_LEARNING_CUES = (
    "root cause",
    "root-cause",
    "root caused",
    "turns out",
    "turned out",
    "the bug was",
    "the bug is",
    "the issue was",
    "the issue is",
    "the problem was",
    "the problem is",
    "the fix was",
    "the fix is",
    "fixed by",
    "caused by",
    "discovered that",
    "discovered the",
    "realized",
    "realised",
    "learned that",
    "the lesson",
    "the mistake was",
    "the mistake is",
    "the reason was",
    "the reason is",
    "figured out",
    "the culprit",
    "boils down to",
    "decided to",
    "should have",
    "the takeaway",
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


def looks_like_user_ask(line: str) -> bool:
    """True when *line* reads like a raw USER directive/request t3 could automate.

    Keyword-blind by design and the structural sibling of
    :func:`looks_like_user_correction`: the signal is the user's own ask prose
    ("can you push the branch", "please open the PR", "let's ship this", or an
    operational lane "hotfix needs to go out asap"), which carries none of
    :data:`TRANSCRIPT_SIGNALS`. Only a USER turn qualifies — the agent's own text
    echoing "can you"/"please" is not a user ask. A bare cue inside an assistant
    line is ignored.
    """
    if _USER_TURN_HINT not in line or not _USER_TURN_RE.search(line):
        return False
    lowered = line.lower()
    return any(cue in lowered for cue in _ASK_CUES)


def looks_like_learning(line: str) -> bool:
    """True when *line* reads like a SUBSTANTIVE learning: a finding, or a decision.

    Keyword-blind of :data:`TRANSCRIPT_SIGNALS` and, unlike the correction/ask
    keepers, ROLE-AGNOSTIC: the declarative drift the literal-signal gate dropped is
    frequently the agent's own recorded finding ("root caused the crash to a missing
    tenant filter"), not only the user's prose — and a lesson is a lesson whichever
    turn states it. Mechanical status chatter ("computed result row 7") and cue-free
    filler carry none of :data:`_LEARNING_CUES`, so the widening keeps substance, not
    volume. Necessary-not-sufficient alongside the other keepers (#2986).
    """
    lowered = line.lower()
    return any(cue in lowered for cue in _LEARNING_CUES)


def _repeated_user_turns(lines: Sequence[str]) -> set[str]:
    user_lines = [line for line in lines if _USER_TURN_HINT in line and _USER_TURN_RE.search(line)]
    counts = Counter(line.strip() for line in user_lines if line.strip())
    return {line for line, count in counts.items() if count > _REPEAT_THRESHOLD}


def high_signal_lines(raw: str) -> str:
    """Keep the lines worth distilling: keyword signals, corrections, asks, or learnings.

    The user-ask keeper rides here too (#2663) so a recurring manual directive
    reaches the distiller and clusters into an automatable-ask gap — otherwise the
    keyword gate would drop a directive that carries no correction cue and no signal
    token before the engine ever saw it. The learning keeper rides here too (#2986):
    a declarative finding/decision (from either role) carries no literal signal token
    and neither a correction nor an ask cue, yet it is the day's richest drift — so the
    keyword gate used to starve it out and a plain pass distilled 0 clusters from a
    corpus full of real learnings.
    """
    lines = raw.splitlines()
    repeated = _repeated_user_turns(lines)
    kept = [
        line
        for line in lines
        if any(signal in line for signal in TRANSCRIPT_SIGNALS)
        or looks_like_user_correction(line)
        or looks_like_user_ask(line)
        or looks_like_learning(line)
        or line.strip() in repeated
    ]
    return "\n".join(kept)


def user_ask_lines(raw: str) -> str:
    """Keep only the USER directive/request lines — the sibling of :func:`high_signal_lines`.

    The narrow extract the automatable-ask classifier reads: every line that
    :func:`looks_like_user_ask` flags, nothing else. Where ``high_signal_lines`` mixes
    asks into the broad distiller input, this isolates the ask signal for the
    Bucket-A/B classification.
    """
    return "\n".join(line for line in raw.splitlines() if looks_like_user_ask(line))


__all__ = [
    "TRANSCRIPT_SIGNALS",
    "high_signal_lines",
    "looks_like_learning",
    "looks_like_user_ask",
    "looks_like_user_correction",
    "user_ask_lines",
]
