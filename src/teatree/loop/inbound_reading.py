"""What one inbound owner message MEANS — resolved by the model, not by a regex.

The routing this replaces was a keyword table: a fixed imperative-verb set, a
status-token list, and a ``why … fail`` regex. It answers "which of three cheap
paths" and cannot answer the question the loop actually needs answered — *is
this a question to answer, an instruction implying work, a correction, an FYI,
or noise?* A table cannot tell "the interest-rate rounding is still wrong" (an
instruction, in the indicative) from "the interest-rate rounding was wrong" (an FYI
about something already fixed), and it read the owner's question "beside that
everything fixed?" as nothing at all.

So the reading is a model turn: ONE clean-room, cheap-tier, tool-free call
through the shared :func:`~teatree.agents.one_shot.run_one_shot` seam, returning
a small JSON object. The turn costs a few hundred tokens and runs once per
logical turn, bounded by the cycle's own batch size.

**The heuristic is the FALLBACK, not the primary.** When the model turn cannot
run (no harness, a timeout, a provider error) or answers unparseably,
:func:`read_inbound` falls back to the keyword classifier and marks the reading
:attr:`ReadingSource.HEURISTIC`. The fallback keeps the original fail-safe
direction — anything it cannot confidently place as noise or a state question
becomes work — because a missed instruction costs the owner's time while a
spurious one costs tokens.

An emoji-only message is read the same way: the owner answering a thread with
👍 / ❌ is giving an approval or a rejection, not sending noise, and the model
resolves which. The heuristic fallback cannot, and treats a bare emoji as noise.
"""

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from teatree.loop.inbound_classifier import AnswerRoute, classify

logger = logging.getLogger(__name__)


class InboundIntent(StrEnum):
    """What the owner is doing with this message."""

    QUESTION = "question"
    INSTRUCTION = "instruction"
    CORRECTION = "correction"
    FYI = "fyi"
    NOISE = "noise"


class ReadingSource(StrEnum):
    """Which reader produced the verdict — the model, or the keyword fallback."""

    MODEL = "model"
    HEURISTIC = "heuristic"


#: Intents that mean "something must be done". A CORRECTION is included on
#: purpose: the owner saying the factory got something wrong is an instruction
#: to fix it, and treating it as commentary is how a correction gets acked and
#: dropped.
_WORK_INTENTS: frozenset[InboundIntent] = frozenset({InboundIntent.INSTRUCTION, InboundIntent.CORRECTION})


@dataclass(frozen=True, slots=True)
class InboundReading:
    """One message's resolved meaning.

    ``answerable`` is meaningful only for :attr:`InboundIntent.QUESTION` and
    means "answerable from teatree's own recorded state, with no investigation".
    ``work_summary`` is the imperative one-liner a dispatched task is named and
    deduplicated by — it is the interpreted request, so two differently-worded
    reports of the same problem collapse onto one lane.
    """

    intent: InboundIntent
    answerable: bool
    work_summary: str
    source: ReadingSource
    rationale: str = ""

    @property
    def implies_work(self) -> bool:
        """Whether this message needs a lane, rather than a reply or nothing.

        A question teatree cannot answer from its own state implies work too —
        answering it requires someone to go and look, which is the same lane.
        """
        if self.intent in _WORK_INTENTS:
            return True
        return self.intent is InboundIntent.QUESTION and not self.answerable

    @property
    def needs_nothing(self) -> bool:
        """Whether the honest response is a reaction and no further action."""
        return self.intent in {InboundIntent.FYI, InboundIntent.NOISE}


#: The reader seam. The cycle and the reply scanner both take one, so a caller
#: can supply a deterministic reading without a model.
type InboundReader = Callable[[str], InboundReading]

_SYSTEM_PROMPT = (
    "You classify one inbound Slack message sent by the owner of an autonomous "
    "software factory to the factory itself. Reply with ONE JSON object and nothing "
    "else, with keys:\n"
    '  "intent": one of "question", "instruction", "correction", "fyi", "noise"\n'
    '  "answerable": true only when intent is "question" AND it can be answered from '
    "the factory's own recorded state (what it is working on, which pull requests are "
    "open, what is blocked) with no investigation\n"
    '  "work_summary": when something must be done, an imperative one-line summary of '
    "the work, normalised so two differently-worded reports of the same problem produce "
    'the SAME summary; otherwise ""\n'
    '  "rationale": at most one short sentence.\n'
    "Definitions: a question ASKS for information. An instruction asks for work, "
    "including in the indicative ('X is still wrong'). A correction says the factory "
    "did something wrong and implies fixing it. An fyi is information with nothing to "
    "do. Noise is a bare thanks or acknowledgement. A message that is only an emoji is "
    "an approval, a rejection, or an answer — classify it by what it approves or "
    "rejects, and never as noise unless it is purely celebratory."
)

_TIMEOUT_SECONDS = 45.0
#: Bound on what is sent to the model. A pasted log is context, not a longer question.
_MAX_PROMPT_CHARS = 4000


def read_inbound(text: str, *, one_shot: Callable[..., str | None] | None = None) -> InboundReading:
    """Resolve *text* to an :class:`InboundReading`; model first, heuristic on failure.

    ``one_shot`` defaults to :func:`~teatree.agents.one_shot.run_one_shot`, bound
    on first use rather than at import: that module reaches the model-client SDK,
    and this one is on the dashboard urlconf's import graph, where a first
    request would otherwise pay ~2.4 s loading 671 SDK modules after the socket
    has already accepted.

    Never raises: every failure of the model turn — including a refused
    credential, which :func:`run_one_shot` deliberately propagates — degrades to
    the heuristic reading. This is a routing decision on the owner's own DM
    surface; an unroutable message must still be routed somewhere, and the
    heuristic's fail-safe direction (ambiguous ⇒ work) is the safe somewhere.
    """
    stripped = text.strip()
    if not stripped:
        return InboundReading(
            intent=InboundIntent.NOISE,
            answerable=False,
            work_summary="",
            source=ReadingSource.HEURISTIC,
            rationale="empty message",
        )
    try:
        from teatree.agents.one_shot import (  # noqa: PLC0415 — deferred: keeps the urlconf off the SDK
            OneShotSpec,
            run_one_shot,
        )

        raw = (one_shot or run_one_shot)(
            stripped[:_MAX_PROMPT_CHARS],
            OneShotSpec(system_prompt=_SYSTEM_PROMPT, timeout_seconds=_TIMEOUT_SECONDS),
        )
    except Exception as exc:  # noqa: BLE001 — a routing decision never fails; it falls back
        logger.warning("Inbound reading turn failed; falling back to the heuristic: %s", exc)
        return _heuristic_reading(stripped)
    reading = _parse(raw)
    if reading is None:
        return _heuristic_reading(stripped)
    return reading


def _parse(raw: str | None) -> InboundReading | None:
    """The model's JSON as an :class:`InboundReading`, or ``None`` when unusable."""
    if not raw:
        return None
    payload = _loads_object(raw)
    if payload is None:
        return None
    intent = _as_intent(payload.get("intent"))
    if intent is None:
        return None
    answerable = payload.get("answerable") is True and intent is InboundIntent.QUESTION
    return InboundReading(
        intent=intent,
        answerable=answerable,
        work_summary=_as_text(payload.get("work_summary"))[:500],
        source=ReadingSource.MODEL,
        rationale=_as_text(payload.get("rationale"))[:500],
    )


def _loads_object(raw: str) -> dict | None:
    """Parse *raw* as a JSON object, tolerating prose around it.

    A cheap-tier turn sometimes wraps its object in a fenced block or a sentence.
    Slicing to the outermost braces recovers the object; anything that still does
    not parse to a mapping is unusable and falls back.
    """
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(raw[start : end + 1])
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _as_intent(value: object) -> InboundIntent | None:
    if not isinstance(value, str):
        return None
    try:
        return InboundIntent(value.strip().lower())
    except ValueError:
        return None


def _as_text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _heuristic_reading(text: str) -> InboundReading:
    """The keyword classifier's verdict, widened to an :class:`InboundReading`.

    Preserves the original fail-safe direction. ``NEEDS_WORK`` splits on the
    presence of a question mark only — that is all the keyword layer can tell —
    and both halves imply work, so the split changes how the message is
    described, never whether it is acted on.
    """
    route = classify(text)
    if route is AnswerRoute.ACK_ONLY:
        return InboundReading(
            intent=InboundIntent.NOISE,
            answerable=False,
            work_summary="",
            source=ReadingSource.HEURISTIC,
            rationale="keyword fallback: acknowledgement",
        )
    if route is AnswerRoute.SIMPLE:
        return InboundReading(
            intent=InboundIntent.QUESTION,
            answerable=True,
            work_summary="",
            source=ReadingSource.HEURISTIC,
            rationale="keyword fallback: state question",
        )
    intent = InboundIntent.QUESTION if "?" in text else InboundIntent.INSTRUCTION
    return InboundReading(
        intent=intent,
        answerable=False,
        work_summary=text[:200],
        source=ReadingSource.HEURISTIC,
        rationale="keyword fallback: needs work",
    )


__all__ = ["InboundIntent", "InboundReader", "InboundReading", "ReadingSource", "read_inbound"]
