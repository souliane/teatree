"""The envelope keys an agent stops on: an owner question names its decision, the facts it checked and its card."""

from typing import TypedDict

from teatree.core.modelkit.owner_decision import OwnerDecision
from teatree.core.modelkit.question_card import OptionDict

type JSONSchema = dict[str, object]

#: What a refusal tells an agent about the card an owner stop must carry.
OWNER_STOP_SHAPE = (
    "Stop for the owner with `user_input_reason` (ONE question ending in `?`), `user_input_kind`, `user_input_checked` "
    '(1-4 facts) and `user_input_card` {"why": "<one sentence>", "blocker": "<what stops you>", "options": '
    '[{"label", "description", "recommended": true}]} (2-4 options, the recommended one first, or none); '
    "at most 120 words in all, plain English, no ids or code."
)


class OwnerCard(TypedDict, total=False):
    why: str
    blocker: str
    options: list[OptionDict]


class OwnerStop(TypedDict, total=False):
    needs_user_input: bool
    user_input_reason: str
    user_input_kind: str
    user_input_checked: list[str]
    user_input_card: OwnerCard


#: Merged into ``RESULT_JSON_SCHEMA["properties"]``.
OWNER_STOP_SCHEMA_PROPERTIES: JSONSchema = {
    "needs_user_input": {"type": "boolean"},
    "user_input_reason": {"type": "string"},
    "user_input_kind": {"type": "string", "enum": [decision.value for decision in OwnerDecision]},
    "user_input_checked": {"type": "array", "items": {"type": "string"}},
    "user_input_card": {
        "type": "object",
        "properties": {
            "why": {"type": "string"},
            "blocker": {"type": "string"},
            "options": {"type": "array", "items": {"type": "object"}},
        },
    },
}
