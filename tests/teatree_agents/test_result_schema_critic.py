"""The critic schema property is wired into the public result envelope."""

from teatree.agents.result_schema import RESULT_JSON_SCHEMA
from teatree.agents.result_schema_critic import CRITIC_VERDICT_SCHEMA_PROPERTY


def test_critic_property_keeps_point_classification_and_verification() -> None:
    properties = RESULT_JSON_SCHEMA["properties"]
    assert isinstance(properties, dict)
    assert properties["critic_verdict"] is CRITIC_VERDICT_SCHEMA_PROPERTY
    item = CRITIC_VERDICT_SCHEMA_PROPERTY["properties"]["items"]["items"]["properties"]
    assert item["verified"] == {"type": "boolean"}
    assert item["points"]["items"]["properties"]["kind"]["enum"] == ["blocking", "nit"]
    assert item["resolutions"]["items"]["properties"]["status"]["enum"] == ["fixed", "not_fixed"]
