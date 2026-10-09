"""The envelope keys an agent stops on: an owner question names its decision and the facts it checked."""

from typing import TypedDict

from teatree.core.modelkit.owner_decision import OwnerDecision

type JSONSchema = dict[str, object]


class OwnerStop(TypedDict, total=False):
    needs_user_input: bool
    user_input_reason: str
    user_input_kind: str
    user_input_checked: list[str]


#: Merged into ``RESULT_JSON_SCHEMA["properties"]``.
OWNER_STOP_SCHEMA_PROPERTIES: JSONSchema = {
    "needs_user_input": {"type": "boolean"},
    "user_input_reason": {"type": "string"},
    "user_input_kind": {"type": "string", "enum": [decision.value for decision in OwnerDecision]},
    "user_input_checked": {"type": "array", "items": {"type": "string"}},
}
