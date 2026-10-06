"""Whole-page discussion enumeration — the union, and the refusal to under-report.

The tree each test builds mirrors a real specification page measured on
2026-09-20: one page-scoped thread and twelve anchored on blocks at three
depths, one of which carries two comments. Against that page the page-scoped
read alone returned 1 comment; the walk returns 14.
"""

from typing import TYPE_CHECKING, cast

import pytest

from teatree.backends.notion.client import NotionClient
from teatree.backends.notion.discussions import (
    UNPROVABLE_BY_THIS_API,
    DiscussionEnumerator,
    NotionIncompleteEnumerationError,
    render_discussions,
)
from tests.teatree_backends.notion._fake_notion import FakeNotion

if TYPE_CHECKING:
    from teatree.types import RawAPIDict


@pytest.fixture
def client() -> NotionClient:
    return NotionClient(token="test-token")


def _spec_page(notion: FakeNotion) -> None:
    """One page-scoped thread plus twelve anchored ones, nested up to three deep."""
    notion.comment_on(notion.page_id, "page-scoped question", discussion_id="disc-page", author="Ben")
    for index in range(4):
        paragraph = notion.paragraph(f"requirement {index}")
        notion.comment_on(paragraph, f"inline {index}", discussion_id=f"disc-inline-{index}", author="Ben")
    toggle = notion.add({"type": "toggle", "toggle": {"rich_text": [], "children": []}})
    for index in range(4):
        nested = notion.paragraph(f"nested {index}", parent=toggle)
        notion.comment_on(nested, f"toggled {index}", discussion_id=f"disc-toggled-{index}", author="bob")
    table = notion.add({"type": "table", "table": {"table_width": 1, "children": []}})
    for index in range(4):
        row = notion.add({"type": "table_row", "table_row": {"cells": []}}, parent=table)
        notion.comment_on(row, f"row {index}", discussion_id=f"disc-row-{index}", author="Adrien")
    notion.comment_on(notion.children[table][0], "reply on the same thread", discussion_id="disc-row-0")


class TestUnionEnumeration:
    def test_block_anchored_threads_are_enumerated_not_only_page_scoped_ones(
        self, notion: FakeNotion, client: NotionClient
    ) -> None:
        _spec_page(notion)

        assert len(client.list_comments(notion.page_id)) == 1

        result = DiscussionEnumerator(client).enumerate(notion.page_id)

        assert len(result.comments) == 14
        assert len(result.discussion_ids) == 13
        assert result.complete

    def test_each_comment_carries_its_anchor_author_and_timestamp(
        self, notion: FakeNotion, client: NotionClient
    ) -> None:
        paragraph = notion.paragraph("the disputed line")
        notion.comment_on(
            paragraph,
            "this contradicts the paragraph above",
            discussion_id="disc-77",
            author="Alice Example",
            created_time="2026-09-16T10:21:00.000Z",
        )

        found = next(c for c in DiscussionEnumerator(client).enumerate(notion.page_id).comments)

        assert found.discussion_id == "disc-77"
        assert found.block_id == paragraph
        assert found.block_type == "paragraph"
        assert found.author == "Alice Example"
        assert found.created_time == "2026-09-16T10:21:00.000Z"
        assert found.text == "this contradicts the paragraph above"

    def test_a_child_page_is_walked_rather_than_named(self, notion: FakeNotion, client: NotionClient) -> None:
        child = notion.add({"type": "child_page", "child_page": {"title": "Sub-spec"}})
        buried = notion.paragraph("buried requirement", parent=child)
        notion.comment_on(buried, "open question on the child", discussion_id="disc-child")

        result = DiscussionEnumerator(client).enumerate(notion.page_id)

        assert "disc-child" in result.discussion_ids

    def test_database_rows_are_walked(self, notion: FakeNotion, client: NotionClient) -> None:
        notion.add({"type": "child_database", "child_database": {"title": "Data points"}})
        notion.rows = [{"id": "row-page-1"}]
        notion.comment_on("row-page-1", "is this field mandatory?", discussion_id="disc-row-page")

        result = DiscussionEnumerator(client).enumerate(notion.page_id)

        assert "disc-row-page" in result.discussion_ids
        assert result.complete

    def test_a_first_page_only_read_would_miss_the_tail(self, notion: FakeNotion, client: NotionClient) -> None:
        notion.children_page_size = 1
        for index in range(5):
            paragraph = notion.paragraph(f"requirement {index}")
            notion.comment_on(paragraph, f"inline {index}", discussion_id=f"disc-{index}")

        result = DiscussionEnumerator(client).enumerate(notion.page_id)

        assert len(result.discussion_ids) == 5
        assert result.complete


class TestLoudIncompleteness:
    def test_an_unreadable_subtree_is_a_named_gap_not_a_shorter_list(
        self, notion: FakeNotion, client: NotionClient
    ) -> None:
        toggle = notion.add({"type": "toggle", "toggle": {"rich_text": [], "children": []}})
        notion.paragraph("unreachable requirement", parent=toggle)
        notion.children_fail_for[toggle] = (404, "object_not_found")

        result = DiscussionEnumerator(client).enumerate(notion.page_id)

        assert not result.complete
        assert [gap.object_id for gap in result.gaps] == [toggle]
        assert result.gaps[0].kind == "children_unreadable"

    def test_an_unreadable_comment_listing_is_a_gap(self, notion: FakeNotion, client: NotionClient) -> None:
        paragraph = notion.paragraph("requirement")
        notion.comments_fail_for[paragraph] = (403, "restricted_resource")

        result = DiscussionEnumerator(client).enumerate(notion.page_id)

        assert not result.complete
        assert result.gaps[0].kind == "comments_unreadable"

    def test_an_unqueryable_embedded_database_is_a_gap(self, notion: FakeNotion, client: NotionClient) -> None:
        database = notion.add({"type": "child_database", "child_database": {"title": "Data points"}})
        notion.query_fail_with = (404, "object_not_found")

        result = DiscussionEnumerator(client).enumerate(notion.page_id)

        assert not result.complete
        assert [(gap.kind, gap.object_id) for gap in result.gaps] == [("database_unreadable", database)]

    def test_a_database_granting_no_data_source_is_a_gap_naming_the_missing_grant(
        self, notion: FakeNotion, client: NotionClient
    ) -> None:
        database = notion.add({"type": "child_database", "child_database": {"title": "Data points"}})
        notion.data_sources = []

        result = DiscussionEnumerator(client).enumerate(notion.page_id)

        assert [(gap.kind, gap.object_id) for gap in result.gaps] == [("database_unreadable", database)]
        assert "share the database with the integration" in result.gaps[0].detail

    def test_the_depth_cap_is_reported_rather_than_silently_truncating(
        self, notion: FakeNotion, client: NotionClient
    ) -> None:
        parent = notion.add({"type": "toggle", "toggle": {"rich_text": [], "children": []}})
        for _ in range(4):
            parent = notion.add({"type": "toggle", "toggle": {"rich_text": [], "children": []}}, parent=parent)

        result = DiscussionEnumerator(client, max_depth=2).enumerate(notion.page_id)

        assert not result.complete
        assert {gap.kind for gap in result.gaps} == {"depth_capped"}

    def test_the_object_cap_is_reported_rather_than_silently_truncating(
        self, notion: FakeNotion, client: NotionClient
    ) -> None:
        for number in range(4):
            notion.paragraph(f"line {number}")

        result = DiscussionEnumerator(client, max_objects=3).enumerate(notion.page_id)

        assert not result.complete
        assert {gap.kind for gap in result.gaps} == {"object_capped"}
        assert result.objects_walked == 3

    def test_an_embedded_database_is_not_paged_past_the_object_cap(
        self, notion: FakeNotion, client: NotionClient
    ) -> None:
        notion.add({"type": "child_database", "child_database": {"title": "Data points"}})
        notion.rows = [{"id": f"row-page-{number}"} for number in range(10)]
        notion.rows_page_size = 1

        result = DiscussionEnumerator(client, max_objects=3).enumerate(notion.page_id)

        assert {gap.kind for gap in result.gaps} == {"object_capped"}
        assert len([path for _method, path in notion.requests if path.endswith("/query")]) <= 2

    def test_an_object_whose_comments_could_not_be_read_is_not_counted_as_read(
        self, notion: FakeNotion, client: NotionClient
    ) -> None:
        paragraph = notion.paragraph("requirement")
        notion.comments_fail_for[paragraph] = (403, "restricted_resource")

        result = DiscussionEnumerator(client).enumerate(notion.page_id)

        assert result.objects_read == 1
        assert "page-level + 0 block(s) scanned, 1 not scanned" in render_discussions(result, as_json=False)

    def test_a_page_whose_own_comments_could_not_be_read_says_so(
        self, notion: FakeNotion, client: NotionClient
    ) -> None:
        notion.paragraph("requirement")
        notion.comments_fail_for[notion.page_id] = (403, "restricted_resource")

        rendered = render_discussions(DiscussionEnumerator(client).enumerate(notion.page_id), as_json=False)

        assert "page-level NOT scanned, 1 block(s) scanned" in rendered

    def test_raise_if_incomplete_refuses_a_partial_set(self, notion: FakeNotion, client: NotionClient) -> None:
        paragraph = notion.paragraph("requirement")
        notion.comments_fail_for[paragraph] = (403, "restricted_resource")

        result = DiscussionEnumerator(client).enumerate(notion.page_id)

        with pytest.raises(NotionIncompleteEnumerationError, match="not the page's full discussion set"):
            result.raise_if_incomplete()

    def test_a_complete_enumeration_does_not_raise(self, notion: FakeNotion, client: NotionClient) -> None:
        _spec_page(notion)

        DiscussionEnumerator(client).enumerate(notion.page_id).raise_if_incomplete()


class TestCrossCheck:
    def test_a_thread_that_vanishes_between_two_reads_is_an_anomaly_not_an_absence(
        self, notion: FakeNotion, client: NotionClient
    ) -> None:
        paragraph = notion.paragraph("the disputed line")
        vanishing = notion.comment_on(paragraph, "the comment the dispute rests on", discussion_id="disc-vanishes")
        notion.vanishing_comment_ids.add(vanishing)

        result = DiscussionEnumerator(client).enumerate(notion.page_id, cross_check=True)

        assert "disc-vanishes" in result.discussion_ids
        assert not result.complete
        assert result.gaps[0].kind == "divergent_reread"
        assert vanishing in result.gaps[0].detail

    def test_a_structural_gap_is_reported_once_not_once_per_read(
        self, notion: FakeNotion, client: NotionClient
    ) -> None:
        notion.add({"type": "child_database", "child_database": {"title": "Data points"}})
        notion.data_sources = []

        result = DiscussionEnumerator(client).enumerate(notion.page_id, cross_check=True)

        assert len(result.gaps) == 1

    def test_a_stable_page_cross_checks_clean(self, notion: FakeNotion, client: NotionClient) -> None:
        _spec_page(notion)

        result = DiscussionEnumerator(client).enumerate(notion.page_id, cross_check=True)

        assert result.complete
        assert len(result.comments) == 14


class TestRendering:
    def test_a_complete_result_still_names_what_the_api_cannot_prove(
        self, notion: FakeNotion, client: NotionClient
    ) -> None:
        _spec_page(notion)

        rendered = render_discussions(DiscussionEnumerator(client).enumerate(notion.page_id), as_json=False)

        assert "COVERAGE: COMPLETE" in rendered
        for caveat in UNPROVABLE_BY_THIS_API:
            assert caveat in rendered

    def test_an_incomplete_result_is_never_presented_as_the_full_set(
        self, notion: FakeNotion, client: NotionClient
    ) -> None:
        paragraph = notion.paragraph("requirement")
        notion.comments_fail_for[paragraph] = (403, "restricted_resource")

        rendered = render_discussions(DiscussionEnumerator(client).enumerate(notion.page_id), as_json=False)

        assert "COVERAGE: INCOMPLETE" in rendered
        assert "comments_unreadable" in rendered
        assert paragraph in rendered

    def test_the_json_form_carries_the_verdict_a_caller_branches_on(
        self, notion: FakeNotion, client: NotionClient
    ) -> None:
        paragraph = notion.paragraph("requirement")
        notion.comment_on(paragraph, "open question", discussion_id="disc-1")
        toggle = notion.add({"type": "toggle", "toggle": {"rich_text": [], "children": []}})
        notion.paragraph("unreachable", parent=toggle)
        notion.children_fail_for[toggle] = (404, "object_not_found")

        payload = DiscussionEnumerator(client).enumerate(notion.page_id).as_dict()

        assert payload["complete"] is False
        assert payload["discussion_count"] == 1
        assert cast("list[RawAPIDict]", payload["gaps"])[0]["kind"] == "children_unreadable"
        assert payload["not_covered_by_this_api"] == list(UNPROVABLE_BY_THIS_API)
