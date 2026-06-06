"""Sync data-type and pure-helper tests (souliane/teatree#443 split of test_sync.py).

Covers SyncResult/DiscussionSummary/PREntry value types and the pure
classify/extract/infer helpers from teatree.backends.gitlab.sync_*.
"""

import pytest

from teatree.backends.gitlab.sync_issues import extract_variant, process_label
from teatree.backends.gitlab.sync_prs import classify_discussions, extract_issue_url, infer_state_from_prs
from teatree.core.models import Ticket
from teatree.types import DiscussionSummary, PREntry, SyncResult
from tests.teatree_core.sync._overlays import _MR_WITH_ISSUE, _MR_WITHOUT_ISSUE, SyncOverlay, _patch_overlay


class TestSyncResult:
    def test_defaults(self) -> None:
        result = SyncResult()
        assert result.labels_fetched == 0
        assert result.errors == []


class TestDiscussionSummary:
    def test_to_dict(self) -> None:
        ds = DiscussionSummary(status="addressed", detail="Fix this")
        assert ds.to_dict() == {"status": "addressed", "detail": "Fix this"}

    def test_frozen(self) -> None:
        ds = DiscussionSummary(status="addressed", detail="Fix this")
        with pytest.raises(AttributeError):
            ds.status = "needs_reply"  # type: ignore[misc]


class TestPREntry:
    def test_required_fields(self) -> None:
        entry = PREntry(
            url="https://example.com/mr/1",
            title="feat: add feature",
            branch="feat/add-feature",
            draft=False,
            repo="backend",
            iid=42,
            updated_at="2026-01-01T00:00:00Z",
        )
        assert entry.url == "https://example.com/mr/1"
        assert entry.iid == 42

    def test_optional_fields_default_to_none(self) -> None:
        entry = PREntry(
            url="u",
            title="t",
            branch="b",
            draft=False,
            repo="r",
            iid=1,
            updated_at="x",
        )
        assert entry.pipeline_status is None
        assert entry.approvals is None
        assert entry.discussions is None
        assert entry.review_permalink is None

    def test_to_dict_omits_none_values(self) -> None:
        entry = PREntry(
            url="u",
            title="t",
            branch="b",
            draft=False,
            repo="r",
            iid=1,
            updated_at="x",
        )
        d = entry.to_dict()
        assert "pipeline_status" not in d
        assert "approvals" not in d
        assert d == {
            "url": "u",
            "title": "t",
            "branch": "b",
            "draft": False,
            "repo": "r",
            "iid": 1,
            "updated_at": "x",
            "state": "opened",
        }

    def test_to_dict_includes_set_optional_fields(self) -> None:
        entry = PREntry(
            url="u",
            title="t",
            branch="b",
            draft=False,
            repo="r",
            iid=1,
            updated_at="x",
            pipeline_status="success",
            pipeline_url="https://pipeline/1",
            review_requested=True,
            reviewer_names=["alice"],
        )
        d = entry.to_dict()
        assert d["pipeline_status"] == "success"
        assert d["review_requested"] is True
        assert d["reviewer_names"] == ["alice"]

    def test_to_dict_serializes_discussions(self) -> None:
        entry = PREntry(
            url="u",
            title="t",
            branch="b",
            draft=False,
            repo="r",
            iid=1,
            updated_at="x",
            discussions=[
                DiscussionSummary(status="addressed", detail="Fix this"),
                DiscussionSummary(status="needs_reply", detail="Please fix"),
            ],
        )
        d = entry.to_dict()
        assert d["discussions"] == [
            {"status": "addressed", "detail": "Fix this"},
            {"status": "needs_reply", "detail": "Please fix"},
        ]

    def test_mutable(self) -> None:
        entry = PREntry(
            url="u",
            title="t",
            branch="b",
            draft=False,
            repo="r",
            iid=1,
            updated_at="x",
        )
        entry.pipeline_status = "success"
        assert entry.pipeline_status == "success"

    def test_draft_comments_fields_default_to_none(self) -> None:
        entry = PREntry(
            url="u",
            title="t",
            branch="b",
            draft=False,
            repo="r",
            iid=1,
            updated_at="x",
        )
        assert entry.draft_comments_pending is None
        assert entry.draft_comments_count is None

    def test_draft_comments_in_to_dict(self) -> None:
        entry = PREntry(
            url="u",
            title="t",
            branch="b",
            draft=False,
            repo="r",
            iid=1,
            updated_at="x",
            draft_comments_pending=True,
            draft_comments_count=3,
        )
        d = entry.to_dict()
        assert d["draft_comments_pending"] is True
        assert d["draft_comments_count"] == 3


class TestExtractIssueUrl:
    def test_from_description(self) -> None:
        assert extract_issue_url(_MR_WITH_ISSUE) == "https://gitlab.com/org/repo/-/issues/100"

    def test_returns_empty_when_none(self) -> None:
        assert extract_issue_url(_MR_WITHOUT_ISSUE) == ""


class TestExtractVariant:
    def test_matches_known_variant(self) -> None:
        """_extract_variant returns the matching known variant (line 424)."""
        overlay = SyncOverlay(known_variants=["Acme", "BigCorp"])
        with _patch_overlay(overlay):
            result = extract_variant(["Bug", "acme", "Priority::High"])
        assert result == "Acme"

    def test_returns_empty_for_unknown(self) -> None:
        """_extract_variant returns '' when no label matches."""
        overlay = SyncOverlay(known_variants=["Acme"])
        with _patch_overlay(overlay):
            result = extract_variant(["Bug", "Priority::High"])
        assert result == ""


class TestProcessLabel:
    def test_returns_none_for_non_process_labels(self) -> None:
        """Labels without Process:: prefix should yield None."""
        assert process_label(["Priority::High", "Bug"]) is None

    def test_returns_none_for_empty_labels(self) -> None:
        assert process_label([]) is None


class TestInferStateFromPrs:
    def test_empty_prs(self) -> None:
        assert infer_state_from_prs({}) == Ticket.State.NOT_STARTED

    def test_corrupted_mrs(self) -> None:
        assert infer_state_from_prs({"x": "not-a-dict"}) == Ticket.State.NOT_STARTED

    def test_draft_mr(self) -> None:
        mrs = {"url1": {"draft": True}}
        assert infer_state_from_prs(mrs) == Ticket.State.STARTED

    def test_non_draft_mr(self) -> None:
        mrs = {"url1": {"draft": False}}
        assert infer_state_from_prs(mrs) == Ticket.State.SHIPPED

    def test_mr_with_approvals(self) -> None:
        mrs = {"url1": {"draft": False, "approvals": {"count": 1, "required": 1}}}
        assert infer_state_from_prs(mrs) == Ticket.State.IN_REVIEW

    def test_mr_with_review_requested(self) -> None:
        mrs = {"url1": {"draft": False, "review_requested": True}}
        assert infer_state_from_prs(mrs) == Ticket.State.IN_REVIEW

    def test_picks_highest_across_mrs(self) -> None:
        mrs = {
            "url1": {"draft": True},  # STARTED
            "url2": {"draft": False, "approvals": {"count": 1, "required": 1}},  # IN_REVIEW
        }
        assert infer_state_from_prs(mrs) == Ticket.State.IN_REVIEW

    def test_second_mr_does_not_advance_when_lower(self) -> None:
        """When second MR infers a lower state than the first, best stays unchanged."""
        mrs = {
            "url1": {"draft": False, "approvals": {"count": 1, "required": 1}},  # IN_REVIEW
            "url2": {"draft": True},  # STARTED (lower)
        }
        # Should pick the highest: IN_REVIEW
        assert infer_state_from_prs(mrs) == Ticket.State.IN_REVIEW


class TestClassifyDiscussions:
    def test_skips_non_dict_entries(self) -> None:
        result = classify_discussions(["not-a-dict", 42], "me")
        assert result == []

    def test_skips_individual_notes(self) -> None:
        result = classify_discussions([{"individual_note": True, "notes": [{"body": "x"}]}], "me")
        assert result == []

    def test_skips_empty_notes(self) -> None:
        result = classify_discussions([{"notes": []}], "me")
        assert result == []

    def test_skips_non_list_notes(self) -> None:
        result = classify_discussions([{"notes": "not-a-list"}], "me")
        assert result == []

    def test_addressed_when_all_resolved(self) -> None:
        discussions = [
            {
                "notes": [
                    {"body": "Fix this", "resolvable": True, "resolved": True, "author": {"username": "reviewer"}},
                ],
            },
        ]
        result = classify_discussions(discussions, "me")
        assert len(result) == 1
        assert result[0] == DiscussionSummary(status="addressed", detail="Fix this")

    def test_waiting_reviewer_when_last_author_is_mr_author(self) -> None:
        discussions = [
            {
                "notes": [
                    {"body": "Fix this", "resolvable": True, "resolved": False, "author": {"username": "reviewer"}},
                    {"body": "Done", "resolvable": False, "author": {"username": "me"}},
                ],
            },
        ]
        result = classify_discussions(discussions, "me")
        assert len(result) == 1
        assert result[0].status == "waiting_reviewer"

    def test_needs_reply_when_last_author_is_not_mr_author(self) -> None:
        discussions = [
            {
                "notes": [
                    {"body": "Please fix", "resolvable": True, "resolved": False, "author": {"username": "reviewer"}},
                ],
            },
        ]
        result = classify_discussions(discussions, "me")
        assert len(result) == 1
        assert result[0].status == "needs_reply"

    def test_non_dict_last_note_author(self) -> None:
        """When the last note is not a dict, the author should be empty -> needs_reply."""
        discussions = [
            {
                "notes": [
                    {"body": "First note", "resolvable": True, "resolved": False, "author": {"username": "reviewer"}},
                    "not-a-dict",
                ],
            },
        ]
        result = classify_discussions(discussions, "me")
        assert result[0].status == "needs_reply"

    def test_non_dict_first_note_body(self) -> None:
        """When the first note is not a dict, first_body should be empty string."""
        discussions = [
            {
                "notes": [
                    "not-a-dict",  # first note, non-dict
                    {"body": "Second", "resolvable": True, "resolved": False, "author": {"username": "reviewer"}},
                ],
            },
        ]
        result = classify_discussions(discussions, "me")
        assert result[0].detail == ""  # first_body from non-dict is ""
