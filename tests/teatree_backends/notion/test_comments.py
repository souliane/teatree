"""The comment write contract: dedup by default, verified by re-read."""

import pytest

from teatree.backends.notion.client import NotionClient
from teatree.backends.notion.comments import CommentPoster
from teatree.backends.notion.errors import NotionWriteNotLandedError
from tests.teatree_backends.notion._fake_notion import FakeNotion

MARKER = "[t3:bdd-test-creation]"
BODY = f"{MARKER} scenarios regenerated from the PRD — 7 total, 2 changed."


def _poster() -> CommentPoster:
    return CommentPoster(NotionClient(token="good"))


def _seed(notion: FakeNotion, text: str) -> None:
    notion.comments.append(
        {
            "object": "comment",
            "id": "comment-seed",
            "discussion_id": "disc-seed",
            "created_by": {"id": "user-7"},
            "created_time": "2026-07-01T00:00:00Z",
            "rich_text": [{"type": "text", "plain_text": text}],
        }
    )


class TestPosting:
    def test_a_first_post_lands_and_reports_the_created_discussion(self, notion: FakeNotion) -> None:
        result = _poster().post(notion.page_id, BODY, marker=MARKER)

        assert result.outcome == "posted"
        assert result.comment_id == notion.comments[-1]["id"]
        assert result.discussion_id == notion.comments[-1]["discussion_id"]
        assert notion.comment_texts() == [BODY]

    def test_the_body_is_stored_literally_rather_than_reparsed_as_markdown(self, notion: FakeNotion) -> None:
        _poster().post(notion.page_id, "**not bold** [t3:x]")

        assert notion.comment_texts() == ["**not bold** [t3:x]"]


class TestIdempotency:
    def test_a_marker_already_on_the_page_is_refused_without_writing(self, notion: FakeNotion) -> None:
        _seed(notion, f"{MARKER} an earlier run said something else entirely")

        result = _poster().post(notion.page_id, BODY, marker=MARKER)

        assert result.outcome == "duplicate"
        assert result.comment_id == "comment-seed"
        assert ("POST", "/comments") not in notion.requests, "a duplicate must never reach Notion"
        assert len(notion.comments) == 1

    def test_a_different_skills_marker_does_not_block_this_one(self, notion: FakeNotion) -> None:
        _seed(notion, "[t3:prd-agent] delivery notes refreshed")

        result = _poster().post(notion.page_id, BODY, marker=MARKER)

        assert result.outcome == "posted"
        assert len(notion.comments) == 2

    def test_the_body_is_the_dedup_key_when_no_marker_is_named(self, notion: FakeNotion) -> None:
        _seed(notion, BODY)

        result = _poster().post(notion.page_id, BODY)

        assert result.outcome == "duplicate"
        assert result.marker == BODY

    def test_allow_duplicate_posts_the_second_copy_deliberately(self, notion: FakeNotion) -> None:
        _seed(notion, BODY)

        result = _poster().post(notion.page_id, BODY, marker=MARKER, allow_duplicate=True)

        assert result.outcome == "posted"
        assert len(notion.comments) == 2

    def test_an_empty_body_is_refused_rather_than_posted_as_a_blank_comment(self, notion: FakeNotion) -> None:
        with pytest.raises(ValueError, match="empty"):
            _poster().post(notion.page_id, "   \n")


class TestVerification:
    def test_a_comment_notion_accepted_but_did_not_store_is_reported_as_failed(self, notion: FakeNotion) -> None:
        notion.suppress_comments = True

        with pytest.raises(NotionWriteNotLandedError, match="treat the write as failed"):
            _poster().post(notion.page_id, BODY, marker=MARKER)
