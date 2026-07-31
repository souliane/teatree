"""Refuse a headless turn-end that carries no usable result envelope.

The instruction is the primary control (:mod:`teatree.agents.envelope_contract`
teaches the contract, and the one-shot lifetime beside it); this is the net under
it. Without the net, a run that ends without the envelope is discovered only by
the recorder — after the turn is over, the context is gone, and the whole run has
to be re-dispatched from scratch. Refusing at ``Stop`` turns that wasted run into
one extra turn, in the same context, with the work still in hand.

It is *satisfiable*, never a filter: the refusal tells the agent what to write,
and the agent writing the real envelope is the only thing that clears it. Nothing
here relaxes the contract or manufactures a result — the recorder's verdict is
unchanged whether this gate fired or not, and the gate reuses the recorder's own
:func:`~teatree.agents.headless_result.parse_result` +
:func:`~teatree.agents.result_schema.check_evidence` so the two can never disagree
about what counts as an envelope.

**Bounded, and it fails OPEN.** It refuses at most
:data:`DEFAULT_ENVELOPE_STOP_REFUSALS` times per run, never re-refuses a turn a
Stop hook already blocked (``stop_hook_active``), and returns "allow" on any
unreadable transcript or unexpected error. A gate that can wedge a dispatch is
worse than the failure it guards, so every uncertain path here lets the turn end.
"""

import json
import logging
from pathlib import Path
from typing import Any, cast

from claude_agent_sdk.types import HookCallback, HookContext, HookJSONOutput, HookMatcher, StopHookInput

from teatree.agents.envelope_refusal import required_keys_phrase
from teatree.agents.headless_result import parse_result
from teatree.agents.result_schema import ProseSummaryPolicy, check_evidence

logger = logging.getLogger(__name__)

#: How many turn-ends one run may have refused before the gate stands down. Two
#: is one correction plus one retry of it: past that the agent is not going to
#: produce the envelope, and holding the run open only burns tokens — the
#: recorder's refusal and the bounded corrective re-dispatch take over.
DEFAULT_ENVELOPE_STOP_REFUSALS = 2


class EnvelopeStopGate:
    """Refuses a run's turn-end while its output carries no usable envelope.

    One instance per headless dispatch — the refusal count is that run's, and a
    fresh dispatch starts from zero. A *limit* of ``0`` (or below) disables the
    gate, matching the ``0 = disabled`` convention of the spawn ceiling and the
    watchdog ceilings.
    """

    def __init__(self, phase: str, *, limit: int = DEFAULT_ENVELOPE_STOP_REFUSALS) -> None:
        self.phase = phase
        self.limit = limit
        self.refused = 0

    @property
    def enabled(self) -> bool:
        """Whether *phase* owes an envelope at all.

        ``scoping`` and ``retro`` may legitimately end on prose
        (:meth:`~teatree.agents.result_schema.ProseSummaryPolicy.accepted`), so
        blocking their turn-end would refuse a run the recorder accepts.
        """
        return self.limit > 0 and not ProseSummaryPolicy.accepted(self.phase)

    async def stop(
        self,
        input_data: StopHookInput,
        tool_use_id: str | None,
        context: HookContext,
    ) -> HookJSONOutput:
        """Allow or refuse one turn-end; any failure to decide allows it."""
        del tool_use_id, context
        try:
            return self._verdict(input_data)
        except Exception:
            logger.exception("envelope stop gate failed; allowing the turn to end")
            return {}

    def _verdict(self, input_data: StopHookInput) -> HookJSONOutput:
        if not self.enabled or input_data.get("stop_hook_active") or self.refused >= self.limit:
            return {}
        agent_text = _assistant_text(input_data.get("transcript_path", ""))
        if not agent_text:
            return {}
        missing = _missing_envelope_reason(agent_text, self.phase)
        if missing is None:
            return {}
        self.refused += 1
        return self._refusal(missing)

    def _refusal(self, missing: str) -> HookJSONOutput:
        reason = (
            f"{missing} This run is ONE-SHOT: ending your turn ends the run, there is no next "
            "turn and no scheduled wakeup, and everything you have done so far is discarded "
            "unrecorded. Finish the work in THIS turn — wait for any command you started to "
            f"complete — then write the result envelope ({required_keys_phrase(self.phase)}) as "
            "one plain JSON object, the last thing you write, with nothing after it."
        )
        logger.warning(
            "envelope stop gate refused a turn-end: phase=%s refused=%d limit=%d",
            self.phase,
            self.refused,
            self.limit,
        )
        return {
            "decision": "block",
            "reason": reason,
            "systemMessage": (
                f"teatree refused a turn-end on phase {self.phase!r}: no usable result envelope "
                f"({self.refused} of {self.limit} refusals used)."
            ),
        }


def _missing_envelope_reason(agent_text: str, phase: str) -> str | None:
    """Why *agent_text* is not a recordable envelope for *phase*, or ``None``.

    Mirrors the recorder's own order — parse first, then the evidence gate — so a
    turn this allows is a turn the recorder accepts.
    """
    result = parse_result(agent_text)
    if not result:
        return "Your output carries no JSON result envelope."
    evidence_error = check_evidence(result, phase)
    if evidence_error:
        return f"Your result envelope is missing this phase's evidence: {evidence_error}."
    return None


def _assistant_text(transcript_path: str) -> str:
    """Every assistant text block in the transcript, oldest first, newline-joined.

    The same projection the runner records as ``agent_text``, so the gate reads
    what the recorder will read. Fail-safe to ``""`` (which allows the turn) on a
    missing, unreadable, or malformed transcript.
    """
    if not transcript_path:
        return ""
    path = Path(transcript_path)
    if not path.is_file():
        return ""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    return "\n".join(text for line in raw.splitlines() for text in _entry_texts(line))


def _entry_texts(raw_line: str) -> list[str]:
    """The assistant text blocks of one transcript JSONL line, or ``[]``."""
    line = raw_line.strip()
    if not line:
        return []
    try:
        entry = json.loads(line)
    except json.JSONDecodeError:
        return []
    if not isinstance(entry, dict):
        return []
    message = entry.get("message")
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return []
    content = message.get("content")
    if not isinstance(content, list):
        return []
    return [
        str(block.get("text", ""))
        for block in content
        if isinstance(block, dict) and block.get("type") == "text" and block.get("text")
    ]


def envelope_stop_hooks(gate: EnvelopeStopGate) -> dict[Any, list[HookMatcher]]:
    """The ``ClaudeAgentOptions.hooks`` bundle that arms *gate* on a dispatch."""
    # The SDK's HookCallback takes the whole HookInput union; this callback is
    # registered on Stop alone, so its narrower parameter is sound here.
    callback = cast("HookCallback", gate.stop)
    return {"Stop": [HookMatcher(hooks=[callback])]}
