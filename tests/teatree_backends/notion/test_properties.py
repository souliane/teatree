"""Page properties — read any type as plain text, write typed by the page itself."""

import pytest

from teatree.backends.notion.client import NotionClient
from teatree.backends.notion.errors import (
    NotionPropertyNotFoundError,
    NotionUnwritablePropertyError,
    NotionWriteNotLandedError,
)
from teatree.backends.notion.properties import (
    PagePropertyWriter,
    build_property_write,
    page_property,
    plain_property_value,
)
from teatree.types import RawAPIDict
from tests.teatree_backends.notion._fake_notion import FakeNotion

REFERENCE = "GitLab Reference"


def _writer() -> PagePropertyWriter:
    return PagePropertyWriter(NotionClient(token="good"))


def _text_property(value: str) -> RawAPIDict:
    return {"type": "rich_text", "rich_text": [{"type": "text", "plain_text": value}]}


class TestReading:
    @pytest.mark.parametrize(
        ("prop", "expected"),
        [
            ({"type": "rich_text", "rich_text": [{"plain_text": "BUG-23"}]}, "BUG-23"),
            ({"type": "title", "title": [{"plain_text": "BUG-12 flaky import"}]}, "BUG-12 flaky import"),
            ({"type": "status", "status": {"name": "In review"}}, "In review"),
            ({"type": "status", "status": None}, ""),
            ({"type": "select", "select": {"name": "Backlog"}}, "Backlog"),
            ({"type": "multi_select", "multi_select": [{"name": "be"}, {"name": "fe"}]}, "be, fe"),
            (
                {"type": "url", "url": "https://gitlab.test/x/-/merge_requests/1"},
                "https://gitlab.test/x/-/merge_requests/1",
            ),
            ({"type": "email", "email": "a@b.test"}, "a@b.test"),
            ({"type": "phone_number", "phone_number": "555-0100"}, "555-0100"),
            ({"type": "number", "number": 12}, "12"),
            ({"type": "number", "number": None}, ""),
            ({"type": "checkbox", "checkbox": True}, "true"),
            ({"type": "checkbox", "checkbox": False}, "false"),
            ({"type": "date", "date": {"start": "2026-07-29", "end": None}}, "2026-07-29"),
            ({"type": "date", "date": {"start": "2026-07-01", "end": "2026-07-29"}}, "2026-07-01 → 2026-07-29"),
            ({"type": "date", "date": None}, ""),
            ({"type": "unique_id", "unique_id": {"prefix": "BUG", "number": 12}}, "BUG-12"),
            ({"type": "created_time", "created_time": "2026-07-29T00:00:00.000Z"}, "2026-07-29T00:00:00.000Z"),
        ],
    )
    def test_each_type_renders_to_the_text_a_headless_caller_branches_on(self, prop: RawAPIDict, expected: str) -> None:
        assert plain_property_value(prop) == expected

    def test_a_type_with_no_plain_form_falls_back_to_its_payload_never_a_silent_empty(self) -> None:
        rendered = plain_property_value({"type": "people", "people": [{"id": "user-7"}]})

        assert rendered == '[{"id": "user-7"}]'

    def test_a_missing_property_names_the_ones_the_page_actually_carries(self) -> None:
        page: RawAPIDict = {"id": "pg-1", "properties": {"Status": {"type": "status"}}}

        with pytest.raises(NotionPropertyNotFoundError, match=r"no property named 'GitLab Reference'.*\['Status'\]"):
            page_property(page, REFERENCE)

    def test_a_page_with_no_properties_at_all_still_fails_loud(self) -> None:
        with pytest.raises(NotionPropertyNotFoundError):
            page_property({"id": "pg-1"}, REFERENCE)


class TestPayloadShapes:
    @pytest.mark.parametrize(
        ("prop_type", "value", "payload", "expected_plain"),
        [
            ("rich_text", "BUG-23", {"rich_text": [{"type": "text", "text": {"content": "BUG-23"}}]}, "BUG-23"),
            ("title", "BUG-12", {"title": [{"type": "text", "text": {"content": "BUG-12"}}]}, "BUG-12"),
            ("status", "Merged", {"status": {"name": "Merged"}}, "Merged"),
            ("select", "Backlog", {"select": {"name": "Backlog"}}, "Backlog"),
            ("multi_select", "be, fe", {"multi_select": [{"name": "be"}, {"name": "fe"}]}, "be, fe"),
            ("url", "https://x.test", {"url": "https://x.test"}, "https://x.test"),
            ("email", "a@b.test", {"email": "a@b.test"}, "a@b.test"),
            ("phone_number", "555-0100", {"phone_number": "555-0100"}, "555-0100"),
            ("number", "12", {"number": 12}, "12"),
            ("number", "12.5", {"number": 12.5}, "12.5"),
            ("checkbox", "yes", {"checkbox": True}, "true"),
            ("checkbox", "false", {"checkbox": False}, "false"),
            ("date", "2026-07-29", {"date": {"start": "2026-07-29"}}, "2026-07-29"),
        ],
    )
    def test_the_payload_shape_comes_from_the_properties_own_type(
        self, prop_type: str, value: str, payload: RawAPIDict, expected_plain: str
    ) -> None:
        write = build_property_write({"type": prop_type}, value)

        assert write.payload == payload
        assert write.expected_plain == expected_plain

    @pytest.mark.parametrize(
        "prop_type",
        ["rich_text", "select", "url", "number", "date", "multi_select"],
    )
    def test_an_empty_value_clears_a_nullable_property(self, prop_type: str) -> None:
        write = build_property_write({"type": prop_type}, "")

        assert write.expected_plain == ""

    @pytest.mark.parametrize("prop_type", ["formula", "rollup", "relation", "people", "files", "created_time"])
    def test_a_type_with_no_plain_text_write_is_refused(self, prop_type: str) -> None:
        with pytest.raises(NotionUnwritablePropertyError, match=prop_type):
            build_property_write({"type": prop_type}, "anything")

    @pytest.mark.parametrize(("prop_type", "value"), [("number", "soon"), ("date", "next week"), ("checkbox", "maybe")])
    def test_a_value_the_type_cannot_hold_is_refused_rather_than_coerced(self, prop_type: str, value: str) -> None:
        with pytest.raises(NotionUnwritablePropertyError, match=value):
            build_property_write({"type": prop_type}, value)


class TestWriting:
    def test_writing_reports_the_previous_value_and_the_landed_one(self, notion: FakeNotion) -> None:
        notion.set_property(REFERENCE, _text_property("BUG-22"))

        result = _writer().write(notion.page_id, name=REFERENCE, value="BUG-23")

        assert result.type == "rich_text"
        assert result.previous == "BUG-22"
        assert result.value == "BUG-23"
        assert plain_property_value(notion.properties[REFERENCE]) == "BUG-23"

    def test_a_status_property_is_written_in_its_own_shape_from_the_same_call(self, notion: FakeNotion) -> None:
        notion.set_property("Status", {"type": "status", "status": {"name": "In review"}})

        result = _writer().write(notion.page_id, name="Status", value="Merged")

        assert result.value == "Merged"
        assert notion.properties["Status"]["status"] == {"name": "Merged"}

    def test_writing_a_property_the_page_does_not_have_never_reaches_notion(self, notion: FakeNotion) -> None:
        with pytest.raises(NotionPropertyNotFoundError):
            _writer().write(notion.page_id, name=REFERENCE, value="BUG-23")

        assert ("PATCH", f"/pages/{notion.page_id}") not in notion.requests

    def test_a_write_notion_accepted_but_did_not_apply_is_reported_as_failed(self, notion: FakeNotion) -> None:
        notion.set_property(REFERENCE, _text_property("BUG-22"))
        notion.suppress_property_writes = True

        with pytest.raises(NotionWriteNotLandedError, match="treat the write as failed"):
            _writer().write(notion.page_id, name=REFERENCE, value="BUG-23")
