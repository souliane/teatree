"""The envelope an answering agent returns can carry exactly the arguments the action table declares (#5685)."""

from teatree.agents.result_schema import RESULT_JSON_SCHEMA
from teatree.agents.result_schema_answering import ACTIONS_SCHEMA_PROPERTY, CoordinatorActionRequest
from teatree.core.modelkit.coordinator_action import ACTION_SPECS


def _declared_names() -> set[str]:
    return {"kind"}.union(*(spec.arguments for spec in ACTION_SPECS.values()))


def test_an_action_entry_carries_the_kind_and_every_argument_any_kind_declares() -> None:
    entry = ACTIONS_SCHEMA_PROPERTY["items"]

    assert isinstance(entry, dict)
    assert set(entry["properties"]) == _declared_names()
    assert entry["required"] == ["kind"]


def test_every_argument_is_a_string() -> None:
    entry = ACTIONS_SCHEMA_PROPERTY["items"]

    assert isinstance(entry, dict)
    assert {prop["type"] for prop in entry["properties"].values()} == {"string"}


def test_the_typed_request_names_the_same_keys_as_the_schema() -> None:
    assert set(CoordinatorActionRequest.__annotations__) == _declared_names()


def test_the_result_schema_offers_the_actions_channel() -> None:
    assert RESULT_JSON_SCHEMA["properties"]["actions"] is ACTIONS_SCHEMA_PROPERTY
