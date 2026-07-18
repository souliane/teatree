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

import json
import re
from collections import Counter
from collections.abc import Sequence
from typing import Any, cast

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
        decode_transcript_line(line)
        for line in lines
        if any(signal in line for signal in TRANSCRIPT_SIGNALS)
        or looks_like_user_correction(line)
        or looks_like_user_ask(line)
        or looks_like_learning(line)
        or line.strip() in repeated
    ]
    return "\n".join(kept)


#: Message-envelope ``type`` / ``role`` values whose content is human prose the
#: distiller cites. A line carrying one of these is decoded to readable text; any
#: other line (a queue-operation, an attachment, a non-JSON marker) is passed
#: through verbatim.
_ROLE_MESSAGE_TYPES = frozenset({"user", "assistant"})


def _as_mapping(value: object) -> dict[str, Any] | None:
    """A JSON object as a ``str``-keyed mapping, or ``None`` — the one ``Any`` chokepoint.

    ``json.loads`` is untyped, so every decoded value arrives as ``object``. Narrowing
    it here (once) keeps the ``Any`` confined to this helper and lets every caller stay
    strictly typed against ``dict[str, Any]``.
    """
    return cast("dict[str, Any]", value) if isinstance(value, dict) else None


def _as_text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _message_role(obj: dict[str, Any]) -> str | None:
    """The ``user`` / ``assistant`` role of a transcript object, across envelope shapes."""
    kind = obj.get("type")
    if isinstance(kind, str) and kind in _ROLE_MESSAGE_TYPES:
        return kind
    message = _as_mapping(obj.get("message"))
    if message is not None:
        role = message.get("role")
        if isinstance(role, str) and role in _ROLE_MESSAGE_TYPES:
            return role
    role = obj.get("role")
    if isinstance(role, str) and role in _ROLE_MESSAGE_TYPES:
        return role
    return None


def _stringify_block(block: object) -> str:
    """Flatten one content block to its human-readable text, adding no re-escaping.

    ``text`` / ``thinking`` blocks carry the prose a citation quotes; a
    ``tool_result`` recurses into its (already-decoded) content so a gate BLOCK /
    DENIED reason survives; a ``tool_use`` contributes only its bare tool name so no
    JSON-escaped input payload re-enters the decoded stream.
    """
    if isinstance(block, str):
        return block
    mapping = _as_mapping(block)
    if mapping is None:
        return ""
    block_type = mapping.get("type")
    if block_type in {"text", "thinking"}:
        return _as_text(mapping.get("text") or mapping.get("thinking") or "")
    if block_type == "tool_result":
        return _stringify_content(mapping.get("content"))
    if block_type == "tool_use":
        return _as_text(mapping.get("name"))
    return _as_text(mapping.get("text"))


def _stringify_content(content: object) -> str:
    """Flatten a message ``content`` (string, or a list of content blocks) to text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(part for part in (_stringify_block(block) for block in content) if part)
    return ""


def _flatten_message_text(obj: dict[str, Any]) -> str:
    """The decoded, human-readable text of a transcript message across envelope shapes."""
    message = _as_mapping(obj.get("message"))
    source = message.get("content") if message is not None else obj.get("content")
    parts = [_stringify_content(source)]
    text = obj.get("text")
    if isinstance(text, str):
        parts.append(text)
    return " ".join(part for part in parts if part).strip()


def decode_transcript_line(line: str) -> str:
    r"""Render one raw JSONL transcript line as decoded, single-line human text.

    Signal DETECTION runs on the RAW escaped JSONL (:func:`high_signal_lines` — the
    regexes need the JSON envelope), but the distiller prompt and the grounding index
    must share ONE decoded representation: a model quotes the human-readable content
    it is shown as its verbatim citation, and that citation can only be a substring of
    the grounding snippet when the snippet holds the SAME decoded form. A raw line
    keeps JSON escapes (``\u2014`` for an em-dash, ``\"`` for ``"``, ``\n`` for a
    newline), so the decoded citation is never a substring of it — the mismatch that
    rejected every transcript-cited cluster as ungrounded.

    A user/assistant turn is flattened to ``{"role": "<role>"} <text>`` on a SINGLE
    line: the JSON role tag is retained so the per-line role heuristics
    (:func:`looks_like_user_correction` / :func:`looks_like_user_ask`, reused by the
    ``compliance`` and weight paths) keep working on the decoded stream, and the
    content is JSON-decoded so a citation grounds. A line that does not parse, is not
    a role message, or carries no extractable text is returned verbatim, so a plain
    signal marker still passes through unchanged.
    """
    try:
        obj = _as_mapping(json.loads(line))
    except (ValueError, TypeError):
        return line
    if obj is None:
        return line
    role = _message_role(obj)
    if role is None:
        return line
    text = _flatten_message_text(obj)
    if not text:
        return line
    return f'{{"role": "{role}"}} {" ".join(text.split())}'


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
    "decode_transcript_line",
    "high_signal_lines",
    "looks_like_learning",
    "looks_like_user_ask",
    "looks_like_user_correction",
    "user_ask_lines",
]
