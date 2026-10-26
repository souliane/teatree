"""Review mismatch uses the public result envelope's strict typed property."""

from teatree.agents.result_schema import RESULT_JSON_SCHEMA
from teatree.agents.result_schema_review import REVIEW_HEAD_MISMATCH_SCHEMA_PROPERTY


def test_mismatch_result_requires_the_observed_head_and_cause() -> None:
    properties = RESULT_JSON_SCHEMA["properties"]
    assert isinstance(properties, dict)
    assert properties["review_head_mismatch"] is REVIEW_HEAD_MISMATCH_SCHEMA_PROPERTY
    assert REVIEW_HEAD_MISMATCH_SCHEMA_PROPERTY["required"] == ["observed_sha", "reason"]
    assert REVIEW_HEAD_MISMATCH_SCHEMA_PROPERTY["additionalProperties"] is False
