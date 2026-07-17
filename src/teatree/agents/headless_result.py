"""Parse, validate, and expose the schema for structured headless-agent result JSON.

Split out of ``headless.py``: turning the agent's final text into a validated
structured result is a self-contained concern, distinct from the run/heartbeat
orchestration the runner owns.
"""

import json

from teatree.agents.result_schema import RESULT_JSON_SCHEMA, AgentResultBlob, JSONSchema


def parse_result(agent_text: str) -> AgentResultBlob:
    """Extract structured result from the agent's text output.

    Tries to parse the last JSON object in the text (agents may print
    progress text before the final JSON result).
    """
    for raw_line in reversed(agent_text.strip().splitlines()):
        stripped = raw_line.strip()
        if stripped.startswith("{"):
            try:
                return json.loads(stripped)
            except json.JSONDecodeError:
                continue
    return {}


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
