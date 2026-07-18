"""Parse, validate, and expose the schema for structured headless-agent result JSON.

Split out of ``headless.py``: turning the agent's final text into a validated
structured result is a self-contained concern, distinct from the run/heartbeat
orchestration the runner owns.
"""

import json
from typing import cast

from teatree.agents.result_schema import RESULT_JSON_SCHEMA, AgentResultBlob, JSONSchema


def parse_result(agent_text: str) -> AgentResultBlob:
    """Return the LAST top-level JSON object in the agent's text output.

    Agents may print progress text before the final JSON result. A line-based
    scan only ever matched single-line JSON — a pretty-printed final object
    spanning several lines never parsed and degraded to truncated prose,
    breaking the #1284 phase-evidence gate. This scans with ``raw_decode`` from
    each ``{``: on a successful top-level decode it jumps past the object (so
    inner braces of a multi-line object are never mistaken for a start) and
    keeps the last dict.
    """
    text = agent_text.strip()
    decoder = json.JSONDecoder()
    best: AgentResultBlob = {}
    index = 0
    while True:
        brace = text.find("{", index)
        if brace == -1:
            return best
        try:
            decoded, end = decoder.raw_decode(text, brace)
        except json.JSONDecodeError:
            index = brace + 1
            continue
        if isinstance(decoded, dict):
            best = cast("AgentResultBlob", decoded)
        index = end


def validate_result(result: AgentResultBlob) -> str:
    """Check that *result* only contains keys declared in the schema.

    Delegates to the shared :func:`~teatree.agents.attempt_recorder.validate_result_keys`
    so the headless and ``record-attempt`` paths enforce the identical
    ``additionalProperties: false`` rule.
    """
    from teatree.agents.attempt_recorder import validate_result_keys  # noqa: PLC0415 — deferred: call-time import

    return validate_result_keys(result)


def get_result_json_schema() -> JSONSchema:
    """Return the JSON schema for structured agent output.

    Agents produce output matching this schema as a final JSON object.
    """
    return RESULT_JSON_SCHEMA
