"""Review mismatch uses the public result envelope's strict typed property."""

from typing import Any, cast

from teatree.agents.result_schema import RESULT_JSON_SCHEMA
from teatree.agents.result_schema_review import (
    HOLD_DISPOSITIONS_SCHEMA_PROPERTY,
    REVIEW_HEAD_MISMATCH_SCHEMA_PROPERTY,
    REVIEW_VENUE_LIMIT_SCHEMA_PROPERTY,
)


def test_mismatch_result_requires_the_observed_head_and_cause() -> None:
    properties = RESULT_JSON_SCHEMA["properties"]
    assert isinstance(properties, dict)
    assert properties["review_head_mismatch"] is REVIEW_HEAD_MISMATCH_SCHEMA_PROPERTY
    assert REVIEW_HEAD_MISMATCH_SCHEMA_PROPERTY["required"] == ["observed_sha", "reason"]
    assert REVIEW_HEAD_MISMATCH_SCHEMA_PROPERTY["additionalProperties"] is False


def test_venue_limit_result_names_its_limits() -> None:
    properties = RESULT_JSON_SCHEMA["properties"]
    assert isinstance(properties, dict)
    assert properties["review_venue_limit"] is REVIEW_VENUE_LIMIT_SCHEMA_PROPERTY
    assert REVIEW_VENUE_LIMIT_SCHEMA_PROPERTY["required"] == ["limits"]


def test_hold_point_answer_can_carry_a_counter_reproduction_with_command_and_result() -> None:
    schema = cast("dict[str, Any]", HOLD_DISPOSITIONS_SCHEMA_PROPERTY)
    point = schema["items"]["properties"]["points"]["items"]
    counter = point["properties"]["counter_reproduction"]

    assert "counter_reproduction" not in point["required"]
    assert counter["required"] == ["command", "observed_result"]
    assert counter["additionalProperties"] is False
