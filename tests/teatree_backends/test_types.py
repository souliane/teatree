from typing import get_type_hints

import pytest

import teatree.backends.types as _types
from teatree.backends.types import Service, dig


class TestService:
    def test_members_cover_the_wrappable_services(self) -> None:
        assert {s.value for s in Service} == {"github", "gitlab", "slack", "notion", "sentry", "sharepoint"}

    def test_round_trips_from_string(self) -> None:
        assert Service("sentry") is Service.SENTRY

    def test_unknown_service_raises(self) -> None:
        with pytest.raises(ValueError, match="figma"):
            Service("figma")


#: The documented field name → annotation of every response shape. These
#: TypedDicts exist as documentation of what a backend returns, so the pin is the
#: exact key set and types — ``issubclass(td, dict)`` is true of ANY TypedDict and
#: pins nothing, so a rename or a dropped field would drift silently.
_RESPONSE_SHAPES: dict[str, dict[str, type]] = {
    "PullRequestResponse": {
        "iid": int,
        "web_url": str,
        "title": str,
        "source_branch": str,
        "target_branch": str,
        "error": str,
    },
    "PipelineResponse": {"id": int, "status": str, "web_url": str, "ref": str, "error": str},
    "QualityCheckResponse": {
        "pipeline_id": int,
        "status": str,
        "total_count": int,
        "success_count": int,
        "failed_count": int,
        "error_count": int,
        "error": str,
    },
    "NoteResponse": {"id": int, "body": str, "error": str},
    "UploadResponse": {"url": str, "markdown": str, "error": str},
    "IssueResponse": {"iid": int, "title": str, "description": str, "state": str},
    "ChatResponse": {"ok": bool, "channel": str, "ts": str},
}


class TestTypedResponses:
    @pytest.mark.parametrize(("name", "fields"), sorted(_RESPONSE_SHAPES.items()))
    def test_field_names_and_types_match_the_documented_shape(self, name: str, fields: dict[str, type]) -> None:
        assert get_type_hints(getattr(_types, name)) == fields

    @pytest.mark.parametrize("name", sorted(_RESPONSE_SHAPES))
    def test_every_key_is_optional(self, name: str) -> None:
        # ``total=False``: a backend fills the subset its call produced, and the
        # ``error`` key is the failure arm — no key is ever required.
        shape = getattr(_types, name)
        assert shape.__required_keys__ == frozenset()
        assert shape.__optional_keys__ == frozenset(_RESPONSE_SHAPES[name])

    def test_the_pin_covers_every_response_shape_the_module_exports(self) -> None:
        exported = {name for name in vars(_types) if name.endswith("Response")}
        assert exported == set(_RESPONSE_SHAPES)


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
