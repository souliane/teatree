"""Parse the final structured result from a headless agent's text output."""

import json
from typing import cast

from teatree.agents.envelope_refusal import UNPARSEABLE_ENVELOPE_PREFIX
from teatree.agents.result_schema import AgentResultBlob


class ResultEnvelopeError(ValueError):
    """A result envelope could not be parsed as a JSON object."""


def _line_state(char: str, previous_nonspace: str, *, at_start: bool) -> tuple[bool, str]:
    if char == "\n":
        return True, previous_nonspace
    if char.isspace():
        return at_start, previous_nonspace
    return False, char


def _quote_state(char: str, *, escaped: bool) -> tuple[bool, bool]:
    if escaped:
        return True, False
    if char == "\\":
        return True, True
    return char != '"', False


def _span_end(text: str, start: int, *, root_object: bool) -> int:
    depth = 0
    quoted = False
    escaped = False
    line_start = text.rfind("\n", 0, start) + 1
    at_line_start = not text[line_start:start].strip()
    previous_nonspace = text[:start].rstrip()[-1:]
    for index in range(start, len(text)):
        char = text[index]
        if (
            index > start
            and char == "{"
            and at_line_start
            and previous_nonspace not in {"", ":"}
            and not (depth > 0 and previous_nonspace == ",")
        ):
            return index
        at_line_start, previous_nonspace = _line_state(char, previous_nonspace, at_start=at_line_start)
        if quoted:
            quoted, escaped = _quote_state(char, escaped=escaped)
            continue
        if char == '"':
            quoted = True
        elif char in "{[":
            depth += 1
        elif char in "}]":
            depth -= 1
            if depth < 0 or (root_object and depth == 0):
                return index + 1
    return len(text)


def parse_result(agent_text: str) -> AgentResultBlob:
    """Return the last complete JSON object, refusing a malformed result-shaped tail."""
    text = agent_text.strip()
    decoder = json.JSONDecoder()
    best: AgentResultBlob = {}
    broken: json.JSONDecodeError | None = None
    index = 0
    while (brace := text.find("{", index)) != -1:
        try:
            decoded, end = decoder.raw_decode(text, brace)
        except json.JSONDecodeError as exc:
            if text[brace + 1 :].lstrip().startswith('"'):
                broken = exc
                index = _span_end(text, brace, root_object=True)
            else:
                index = brace + 1
            continue
        if text[end:].lstrip()[:1] in {",", "}"}:
            broken = json.JSONDecodeError("Extra data", text, end)
            index = _span_end(text, end, root_object=False)
            continue
        best = cast("AgentResultBlob", decoded)
        broken = None
        index = end
    if broken is not None:
        detail = broken.msg.removesuffix(" at")
        message = f"{UNPARSEABLE_ENVELOPE_PREFIX}{detail} at char {broken.pos}"
        raise ResultEnvelopeError(message) from broken
    return best
