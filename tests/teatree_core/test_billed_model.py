import pytest

from teatree.core.billed_model import dominant_model

_AUXILIARY_HAIKU_FIRST = {
    "claude-haiku-4-5-20251001": {"inputTokens": 900, "outputTokens": 40},
    "claude-sonnet-5-5": {"outputTokens": 17000, "cacheReadInputTokens": 400000},
}


def test_the_main_model_wins_over_the_auxiliary_model_claude_code_lists_first() -> None:
    assert dominant_model(_AUXILIARY_HAIKU_FIRST) == "claude-sonnet-5-5"


def test_cache_writes_count_toward_the_volume() -> None:
    assert dominant_model({"a": {"inputTokens": 10}, "b": {"cacheCreationInputTokens": 11}}) == "b"


def test_a_lone_key_with_no_breakdown_is_the_billed_model() -> None:
    assert dominant_model({"claude-opus-5-5": {}}) == "claude-opus-5-5"


def test_non_integer_counts_weigh_nothing() -> None:
    assert dominant_model({"a": {"inputTokens": 5}, "b": {"inputTokens": "9", "outputTokens": True}, "c": []}) == "a"


@pytest.mark.parametrize("model_usage", [None, {}, [1, 2], "claude-opus-5-5"])
def test_an_absent_or_malformed_map_has_no_billed_model(model_usage: object) -> None:
    assert dominant_model(model_usage) is None
