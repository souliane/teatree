"""The comment write contract: dedup by default, verified by re-read."""

import pytest

from teatree.backends.notion.client import NotionClient
from teatree.backends.notion.comments import CommentPoster
from teatree.backends.notion.errors import NotionWriteNotLandedError, NotionWriteRefusedError
from tests.teatree_backends.notion._fake_notion import FakeNotion

MARKER = "[t3:scenario-writer]"
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
        _seed(notion, "[t3:spec-writer] delivery notes refreshed")

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


class TestTheDefaultKeyIsTheWholeComment:
    def test_a_body_contained_in_a_longer_comment_is_not_a_duplicate(self, notion: FakeNotion) -> None:
        _seed(notion, "Is the reset monthly? Yes or no")

        result = _poster().post(notion.page_id, "Yes")

        assert result.outcome == "posted"

    def test_a_short_reply_contained_in_an_earlier_comment_of_its_discussion_is_posted(
        self, notion: FakeNotion
    ) -> None:
        paragraph = notion.paragraph("rates reset every quarter")
        notion.comment_on(paragraph, "Is the reset monthly? Yes or no", discussion_id="disc-1")

        result = _poster().reply(paragraph, "disc-1", "Yes")

        assert result.outcome == "posted"

    def test_the_same_reply_in_another_discussion_on_the_block_is_posted(self, notion: FakeNotion) -> None:
        paragraph = notion.paragraph("rates reset every quarter")
        notion.comment_on(paragraph, "Done.", discussion_id="disc-1")
        notion.comment_on(paragraph, "please confirm", discussion_id="disc-2")

        result = _poster().reply(paragraph, "disc-2", "Done.")

        assert result.outcome == "posted"
        assert notion.comments[-1]["discussion_id"] == "disc-2"

    def test_the_same_reply_again_in_its_own_discussion_is_a_duplicate(self, notion: FakeNotion) -> None:
        paragraph = notion.paragraph("rates reset every quarter")
        notion.comment_on(paragraph, "please confirm", discussion_id="disc-1")
        _poster().reply(paragraph, "disc-1", "Done.")

        result = _poster().reply(paragraph, "disc-1", "  done. ")

        assert result.outcome == "duplicate"


class TestDryRun:
    def test_a_dry_run_names_what_it_would_post_and_sends_no_write(self, notion: FakeNotion) -> None:
        result = _poster().post(notion.page_id, BODY, marker=MARKER, dry_run=True)

        assert result.outcome == "would post"
        assert result.marker == MARKER
        assert {method for method, _path in notion.requests} == {"GET"}
        assert notion.comments == []

    def test_a_dry_run_reports_a_marker_already_on_the_page_as_a_duplicate(self, notion: FakeNotion) -> None:
        _seed(notion, f"{MARKER} an earlier run")

        result = _poster().post(notion.page_id, BODY, marker=MARKER, dry_run=True)

        assert result.outcome == "duplicate"
        assert result.comment_id == "comment-seed"

    def test_a_dry_run_on_a_page_outside_the_write_roots_is_refused(
        self, notion: FakeNotion, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("teatree.backends.notion.write_guard.notion_write_roots", lambda _overlay: ([], []))

        with pytest.raises(NotionWriteRefusedError):
            _poster().post(notion.page_id, BODY, marker=MARKER, dry_run=True)

    def test_a_dry_run_reply_names_its_discussion(self, notion: FakeNotion) -> None:
        paragraph = notion.paragraph("rates reset every quarter")
        notion.comment_on(paragraph, "please confirm", discussion_id="disc-1")

        result = _poster().reply(paragraph, "disc-1", "Done.", dry_run=True)

        assert (result.outcome, result.discussion_id) == ("would post", "disc-1")
