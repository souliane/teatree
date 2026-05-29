import pytest

import teatree.backends.types as _types
from teatree.backends.types import dig


def test_typed_responses_importable() -> None:
    for name in (
        "PullRequestResponse",
        "PipelineResponse",
        "QualityCheckResponse",
        "NoteResponse",
        "UploadResponse",
        "IssueResponse",
        "ChatResponse",
    ):
        td = getattr(_types, name)
        assert issubclass(td, dict)


class TestDig:
    def test_returns_nested_value(self) -> None:
        assert dig({"a": {"b": {"c": 7}}}, "a", "b", "c") == 7

    def test_returns_intermediate_mapping(self) -> None:
        assert dig({"a": {"b": {"c": 7}}}, "a", "b") == {"c": 7}

    @pytest.mark.parametrize(
        "data",
        [
            {"a": None},
            {"a": {"b": None}},
            {"a": "scalar"},
            {},
            None,
        ],
    )
    def test_returns_none_on_missing_or_null_hop(self, data: object) -> None:
        # The bug class this guards: a chained ``.get(k, {})`` calls ``.get`` on
        # a present-but-null value and crashes; ``dig`` returns ``None`` instead.
        assert dig(data, "a", "b", "c") is None
