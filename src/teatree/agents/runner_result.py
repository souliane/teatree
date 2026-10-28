"""Parse the final structured result from a headless agent's text output."""

import json
from typing import cast

from teatree.agents.envelope_refusal import UNPARSEABLE_ENVELOPE_PREFIX
from teatree.agents.result_schema import AgentResultBlob


class ResultEnvelopeError(ValueError):
    """A result envelope could not be parsed as a JSON object."""


def _span_end(text: str, start: int) -> int:
    depth = 0
    quoted = False
    escaped = False
    object_start = text[start] in "{["
    for index in range(start, len(text)):
        char = text[index]
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            continue
        if char == '"':
            quoted = True
        elif char in "{[":
            depth += 1
        elif char in "}]":
            depth -= 1
            if depth < 0 or (object_start and depth == 0):
                return index + 1
    return len(text)


def parse_result(agent_text: str) -> AgentResultBlob:
    """Return the last complete JSON object, refusing a malformed result-shaped tail."""
    text = agent_text.strip()
    decoder = json.JSONDecoder()
    best: AgentResultBlob = {}
    broken: json.JSONDecodeError | None = None
    skip_until = 0
    index = 0
    while (brace := text.find("{", index)) != -1:
        if brace < skip_until:
            index = brace + 1
            continue
        try:
            decoded, end = decoder.raw_decode(text, brace)
        except json.JSONDecodeError as exc:
            if text[brace + 1 :].lstrip().startswith('"'):
                broken = exc
                skip_until = _span_end(text, brace)
            index = brace + 1
            continue
        if text[end:].lstrip()[:1] in {",", "}"}:
            broken = json.JSONDecodeError("Extra data", text, end)
            skip_until = _span_end(text, end)
            index = end
            continue
        best = cast("AgentResultBlob", decoded)
        broken = None
        index = end
    if broken is not None:
        message = f"{UNPARSEABLE_ENVELOPE_PREFIX}{broken.msg} at char {broken.pos}"
        raise ResultEnvelopeError(message) from broken
    return best
