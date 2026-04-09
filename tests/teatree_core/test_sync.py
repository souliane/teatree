from collections.abc import Iterator
from unittest.mock import MagicMock, patch

import pytest
from django.core.cache import cache
from django.test import TestCase

import teatree.core.overlay_loader as overlay_loader_mod
from teatree.backends.gitlab_api import ProjectInfo
from teatree.core.models import Ticket, Worktree
from teatree.core.overlay import OverlayBase, OverlayConfig, ProvisionStep
from teatree.core.overlay_loader import reset_overlay_cache
from teatree.core.sync import (
    LAST_SYNC_CACHE_KEY,
    PENDING_REVIEWS_CACHE_KEY,
    DiscussionSummary,
    MREntry,
    MREntryDict,
    RawAPIDict,
    SyncResult,
    _apply_merged_status,
    _classify_discussions,
    _detect_e2e_evidence,
    _extract_issue_url,
    _extract_variant,
    _fetch_review_permalinks,
    _infer_state_from_mrs,
    _merge_results,
    _merge_ticket_extras,
    _overlay_name,
    _process_label,
    _resolve_issue,
    _sync_reviewer_mrs,
    _update_ticket,
    fetch_notion_statuses,
    sync_followup,
)

# ---------------------------------------------------------------------------
# Test overlay classes
# ---------------------------------------------------------------------------


class SyncConfig(OverlayConfig):
    """Configurable overlay config for sync tests."""

    def __init__(  # noqa: PLR0913
        self,
        *,
        gitlab_token: str = "test-token",  # noqa: S107
        gitlab_username: str = "testuser",
        github_token: str = "",
        github_owner: str = "",
        github_project_number: int = 0,
        slack_token: str = "",
        review_channel: tuple[str, str] = ("", ""),
        known_variants: list[str] | None = None,
    ) -> None:
        self._gitlab_token = gitlab_token
        self._gitlab_username = gitlab_username
        self._github_token = github_token
        self.github_owner = github_owner
        self.github_project_number = github_project_number
        self._slack_token = slack_token
        self._review_channel = review_channel
        self.known_variants = known_variants or []

    def get_gitlab_token(self) -> str:
        return self._gitlab_token

    def get_github_token(self) -> str:
        return self._github_token

    def get_gitlab_username(self) -> str:
        return self._gitlab_username

    def get_slack_token(self) -> str:
        return self._slack_token

    def get_review_channel(self) -> tuple[str, str]:
        return self._review_channel


class SyncOverlay(OverlayBase):
    """Overlay for sync tests with configurable GitLab/Slack/variant settings."""

    def __init__(self, **config_kwargs: object) -> None:
        self.config = SyncConfig(**config_kwargs)

    def get_repos(self) -> list[str]:
        return ["backend"]

    def get_provision_steps(self, worktree: object) -> list[ProvisionStep]:
        return []


def _patch_overlay(overlay: OverlayBase, *, name: str = "test"):
    """Return a ``patch`` that makes the overlay loader return the given instance."""
    result: dict[str, OverlayBase] = {name: overlay}

    def _fake_discover() -> dict[str, OverlayBase]:
        return result

    _fake_discover.cache_clear = lambda: None

    return patch.object(overlay_loader_mod, "_discover_overlays", new=_fake_discover)


@pytest.fixture(autouse=True)
def _clear_overlay_cache() -> Iterator[None]:
    reset_overlay_cache()
    yield
    reset_overlay_cache()


_PROJECT = ProjectInfo(project_id=123, path_with_namespace="org/repo", short_name="repo")

_MR_WITH_ISSUE = {
    "web_url": "https://gitlab.com/org/repo/-/merge_requests/42",
    "title": "feat: add feature",
    "description": "feat: add feature [none] (https://gitlab.com/org/repo/-/issues/100)\n\nBody",
    "source_branch": "feat/add-feature",
    "draft": False,
    "iid": 42,
    "project_id": 123,
}

_MR_WITHOUT_ISSUE = {
    "web_url": "https://gitlab.com/org/repo/-/merge_requests/43",
    "title": "fix: quick patch",
    "description": "fix: quick patch",
    "source_branch": "fix/quick-patch",
    "draft": True,
    "iid": 43,
    "project_id": 123,
}

_MR_WITH_WORK_ITEM = {
    "web_url": "https://gitlab.com/org/repo/-/merge_requests/44",
    "title": "feat: work item feature",
    "description": "feat: work item feature (https://gitlab.com/org/repo/-/work_items/200)\n\nBody",
    "source_branch": "feat/work-item",
    "draft": False,
    "iid": 44,
    "project_id": 123,
}

_MERGED_MR = {
    "web_url": "https://gitlab.com/org/repo/-/merge_requests/42",
    "iid": 42,
    "project_id": 123,
}


def _make_mock_client(mrs: list[dict]) -> MagicMock:
    mock = MagicMock()
    mock.list_open_mrs.return_value = mrs
    mock.list_all_open_mrs.return_value = mrs
    mock.list_recently_merged_mrs.return_value = []
    mock.resolve_project.return_value = _PROJECT
    mock.get_mr_pipeline.return_value = {"status": "success", "url": "https://gitlab.com/pipelines/1"}
    mock.get_mr_approvals.return_value = {"count": 0, "required": 1}
    mock.get_issue.return_value = {"labels": ["Process::Doing"], "title": "Issue title"}
    return mock


def _make_merged_mock(merged_mrs: list[dict]) -> MagicMock:
    """Mock client with no open MRs and some merged MRs."""
    mock = _make_mock_client([])
    mock.list_recently_merged_mrs.return_value = merged_mrs
    return mock


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


class TestMREntry:
    def test_required_fields(self) -> None:
        entry = MREntry(
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
        entry = MREntry(
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
        entry = MREntry(
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
        }

    def test_to_dict_includes_set_optional_fields(self) -> None:
        entry = MREntry(
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
        entry = MREntry(
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
        entry = MREntry(
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


class TestExtractIssueUrl:
    def test_from_description(self) -> None:
        assert _extract_issue_url(_MR_WITH_ISSUE) == "https://gitlab.com/org/repo/-/issues/100"

    def test_returns_empty_when_none(self) -> None:
        assert _extract_issue_url(_MR_WITHOUT_ISSUE) == ""


class TestExtractVariant:
    def test_matches_known_variant(self) -> None:
        """_extract_variant returns the matching known variant (line 424)."""
        overlay = SyncOverlay(known_variants=["Acme", "BigCorp"])
        with _patch_overlay(overlay):
            result = _extract_variant(["Bug", "acme", "Priority::High"])
        assert result == "Acme"

    def test_returns_empty_for_unknown(self) -> None:
        """_extract_variant returns '' when no label matches."""
        overlay = SyncOverlay(known_variants=["Acme"])
        with _patch_overlay(overlay):
            result = _extract_variant(["Bug", "Priority::High"])
        assert result == ""


class TestProcessLabel:
    def test_returns_none_for_non_process_labels(self) -> None:
        """Labels without Process:: prefix should yield None."""
        assert _process_label(["Priority::High", "Bug"]) is None

    def test_returns_none_for_empty_labels(self) -> None:
        assert _process_label([]) is None


class TestInferStateFromMrs:
    def test_empty_mrs(self) -> None:
        assert _infer_state_from_mrs({}) == Ticket.State.NOT_STARTED

    def test_corrupted_mrs(self) -> None:
        assert _infer_state_from_mrs({"x": "not-a-dict"}) == Ticket.State.NOT_STARTED

    def test_draft_mr(self) -> None:
        mrs = {"url1": {"draft": True}}
        assert _infer_state_from_mrs(mrs) == Ticket.State.STARTED

    def test_non_draft_mr(self) -> None:
        mrs = {"url1": {"draft": False}}
        assert _infer_state_from_mrs(mrs) == Ticket.State.SHIPPED

    def test_mr_with_approvals(self) -> None:
        mrs = {"url1": {"draft": False, "approvals": {"count": 1, "required": 1}}}
        assert _infer_state_from_mrs(mrs) == Ticket.State.IN_REVIEW

    def test_mr_with_review_requested(self) -> None:
        mrs = {"url1": {"draft": False, "review_requested": True}}
        assert _infer_state_from_mrs(mrs) == Ticket.State.IN_REVIEW

    def test_picks_highest_across_mrs(self) -> None:
        mrs = {
            "url1": {"draft": True},  # STARTED
            "url2": {"draft": False, "approvals": {"count": 1, "required": 1}},  # IN_REVIEW
        }
        assert _infer_state_from_mrs(mrs) == Ticket.State.IN_REVIEW

    def test_second_mr_does_not_advance_when_lower(self) -> None:
        """When second MR infers a lower state than the first, best stays unchanged."""
        mrs = {
            "url1": {"draft": False, "approvals": {"count": 1, "required": 1}},  # IN_REVIEW
            "url2": {"draft": True},  # STARTED (lower)
        }
        # Should pick the highest: IN_REVIEW
        assert _infer_state_from_mrs(mrs) == Ticket.State.IN_REVIEW


class TestClassifyDiscussions:
    def test_skips_non_dict_entries(self) -> None:
        result = _classify_discussions(["not-a-dict", 42], "me")
        assert result == []

    def test_skips_individual_notes(self) -> None:
        result = _classify_discussions([{"individual_note": True, "notes": [{"body": "x"}]}], "me")
        assert result == []

    def test_skips_empty_notes(self) -> None:
        result = _classify_discussions([{"notes": []}], "me")
        assert result == []

    def test_skips_non_list_notes(self) -> None:
        result = _classify_discussions([{"notes": "not-a-list"}], "me")
        assert result == []

    def test_addressed_when_all_resolved(self) -> None:
        discussions = [
            {
                "notes": [
                    {"body": "Fix this", "resolvable": True, "resolved": True, "author": {"username": "reviewer"}},
                ],
            }
        ]
        result = _classify_discussions(discussions, "me")
        assert len(result) == 1
        assert result[0] == DiscussionSummary(status="addressed", detail="Fix this")

    def test_waiting_reviewer_when_last_author_is_mr_author(self) -> None:
        discussions = [
            {
                "notes": [
                    {"body": "Fix this", "resolvable": True, "resolved": False, "author": {"username": "reviewer"}},
                    {"body": "Done", "resolvable": False, "author": {"username": "me"}},
                ],
            }
        ]
        result = _classify_discussions(discussions, "me")
        assert len(result) == 1
        assert result[0].status == "waiting_reviewer"

    def test_needs_reply_when_last_author_is_not_mr_author(self) -> None:
        discussions = [
            {
                "notes": [
                    {"body": "Please fix", "resolvable": True, "resolved": False, "author": {"username": "reviewer"}},
                ],
            }
        ]
        result = _classify_discussions(discussions, "me")
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
            }
        ]
        result = _classify_discussions(discussions, "me")
        assert result[0].status == "needs_reply"

    def test_non_dict_first_note_body(self) -> None:
        """When the first note is not a dict, first_body should be empty string."""
        discussions = [
            {
                "notes": [
                    "not-a-dict",  # first note, non-dict
                    {"body": "Second", "resolvable": True, "resolved": False, "author": {"username": "reviewer"}},
                ],
            }
        ]
        result = _classify_discussions(discussions, "me")
        assert result[0].detail == ""  # first_body from non-dict is ""


class TestUpdateTicket(TestCase):
    def test_preserves_skill_written_fields(self) -> None:
        """Skill-written fields (review_channel, review_permalink, e2e_test_plan_url) survive sync updates."""
        ticket = Ticket.objects.create(
            overlay="test",
            issue_url="https://gitlab.com/org/repo/-/issues/200",
            repos=["repo"],
            extra={
                "mrs": {
                    "https://gitlab.com/org/repo/-/merge_requests/50": {
                        "url": "https://gitlab.com/org/repo/-/merge_requests/50",
                        "repo": "repo",
                        "title": "feat: old title",
                        "review_channel": "#backend-review",
                        "review_permalink": "https://slack.com/archives/C123/p456",
                        "e2e_test_plan_url": "https://gitlab.com/org/repo/-/merge_requests/50#note_789",
                    },
                },
            },
        )

        # Simulate a sync update that doesn't include the skill-written fields
        new_mr_entry: MREntryDict = {
            "url": "https://gitlab.com/org/repo/-/merge_requests/50",
            "repo": "repo",
            "title": "feat: new title",
            "pipeline_status": "success",
        }

        _update_ticket(ticket, new_mr_entry, "https://gitlab.com/org/repo/-/merge_requests/50", "repo")

        ticket.refresh_from_db()
        mr = ticket.extra["mrs"]["https://gitlab.com/org/repo/-/merge_requests/50"]
        assert mr["title"] == "feat: new title"
        assert mr["review_channel"] == "#backend-review"
        assert mr["review_permalink"] == "https://slack.com/archives/C123/p456"
        assert mr["e2e_test_plan_url"] == "https://gitlab.com/org/repo/-/merge_requests/50#note_789"


class TestMergeTicketExtras(TestCase):
    def test_combines_mrs_and_repos(self) -> None:
        """_merge_ticket_extras merges MR entries and repos from source into target."""
        target = Ticket.objects.create(
            overlay="test",
            issue_url="https://gitlab.com/org/repo/-/issues/900",
            repos=["repo-a"],
            extra={"mrs": {"https://mr/1": {"title": "MR 1"}}},
        )
        source = Ticket.objects.create(
            overlay="test",
            issue_url="https://gitlab.com/org/repo/-/issues/901",
            repos=["repo-b"],
            extra={"mrs": {"https://mr/2": {"title": "MR 2"}}},
        )
        _merge_ticket_extras(target, source)
        target.refresh_from_db()

        assert "https://mr/1" in target.extra["mrs"]
        assert "https://mr/2" in target.extra["mrs"]
        assert "repo-a" in target.repos
        assert "repo-b" in target.repos

    def test_handles_non_dict_mrs(self) -> None:
        """Non-dict mrs in extras are treated as empty -- repos still merge."""
        target = Ticket.objects.create(
            overlay="test",
            issue_url="https://gitlab.com/org/repo/-/issues/960",
            repos=["repo-a"],
            extra={"mrs": "corrupt"},
        )
        source = Ticket.objects.create(
            overlay="test",
            issue_url="https://gitlab.com/org/repo/-/issues/961",
            repos=["repo-b"],
            extra={"mrs": ["also-corrupt"]},
        )
        _merge_ticket_extras(target, source)
        target.refresh_from_db()
        assert target.repos == ["repo-a", "repo-b"]

    def test_skips_overlapping_mrs_and_repos(self) -> None:
        """Overlapping MR URLs and repos are not duplicated."""
        target = Ticket.objects.create(
            overlay="test",
            issue_url="https://gitlab.com/org/repo/-/issues/950",
            repos=["repo-a", "repo-b"],
            extra={"mrs": {"https://mr/1": {"title": "MR 1"}}},
        )
        source = Ticket.objects.create(
            overlay="test",
            issue_url="https://gitlab.com/org/repo/-/issues/951",
            repos=["repo-b", "repo-c"],
            extra={"mrs": {"https://mr/1": {"title": "MR 1 dup"}, "https://mr/3": {"title": "MR 3"}}},
        )
        _merge_ticket_extras(target, source)
        target.refresh_from_db()

        assert target.extra["mrs"]["https://mr/1"]["title"] == "MR 1"
        assert "https://mr/3" in target.extra["mrs"]
        assert target.repos == ["repo-a", "repo-b", "repo-c"]


class TestFetchReviewPermalinks(TestCase):
    _SLACK_OVERLAY = SyncOverlay(
        slack_token="xoxb-token",
        review_channel=("review-crew", "C123"),
    )

    @pytest.fixture(autouse=True)
    def _inject_fixtures(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._monkeypatch = monkeypatch

    def test_returns_early_without_token(self) -> None:
        """_fetch_review_permalinks returns early when no token (line 359)."""
        overlay = SyncOverlay(slack_token="", review_channel=("", ""))
        with _patch_overlay(overlay):
            result = SyncResult()
            _fetch_review_permalinks(result)
        assert result.reviews_synced == 0
        assert result.errors == []

    def test_skips_draft_mrs(self) -> None:
        """_fetch_review_permalinks skips draft MRs (line 374)."""
        Ticket.objects.create(
            overlay="test",
            issue_url="https://gitlab.com/org/repo/-/issues/500",
            repos=["repo"],
            state=Ticket.State.STARTED,
            extra={
                "mrs": {
                    "https://gitlab.com/org/repo/-/merge_requests/50": {
                        "draft": True,
                        "url": "https://gitlab.com/org/repo/-/merge_requests/50",
                    },
                },
            },
        )

        with _patch_overlay(self._SLACK_OVERLAY):
            result = SyncResult()
            _fetch_review_permalinks(result)
        # No non-draft MRs -> no Slack call
        assert result.reviews_synced == 0

    def test_skips_already_linked_mrs(self) -> None:
        """_fetch_review_permalinks skips MRs that already have review_permalink (line 376-377)."""
        Ticket.objects.create(
            overlay="test",
            issue_url="https://gitlab.com/org/repo/-/issues/501",
            repos=["repo"],
            state=Ticket.State.SHIPPED,
            extra={
                "mrs": {
                    "https://gitlab.com/org/repo/-/merge_requests/51": {
                        "draft": False,
                        "review_permalink": "https://slack.com/existing",
                    },
                },
            },
        )

        with _patch_overlay(self._SLACK_OVERLAY):
            result = SyncResult()
            _fetch_review_permalinks(result)
        assert result.reviews_synced == 0

    def test_returns_early_when_no_urls(self) -> None:
        """_fetch_review_permalinks returns early when no eligible MR URLs (line 382-383)."""
        with _patch_overlay(self._SLACK_OVERLAY):
            result = SyncResult()
            _fetch_review_permalinks(result)
        assert result.reviews_synced == 0

    def test_handles_search_exception(self) -> None:
        """_fetch_review_permalinks appends error on exception (line 392-393)."""
        Ticket.objects.create(
            overlay="test",
            issue_url="https://gitlab.com/org/repo/-/issues/502",
            repos=["repo"],
            state=Ticket.State.SHIPPED,
            extra={
                "mrs": {
                    "https://gitlab.com/org/repo/-/merge_requests/52": {
                        "draft": False,
                    },
                },
            },
        )

        def _explode(**kw: object) -> list:
            msg = "Slack timeout"
            raise RuntimeError(msg)

        self._monkeypatch.setattr("teatree.backends.slack.search_review_permalinks", _explode)

        with _patch_overlay(self._SLACK_OVERLAY):
            result = SyncResult()
            _fetch_review_permalinks(result)
        assert any("Slack review sync" in e for e in result.errors)

    def test_stores_matches(self) -> None:
        """_fetch_review_permalinks updates ticket extra with permalink (lines 396-410)."""
        from teatree.backends.slack import SlackReviewMatch  # noqa: PLC0415

        mr_url = "https://gitlab.com/org/repo/-/merge_requests/53"
        Ticket.objects.create(
            overlay="test",
            issue_url="https://gitlab.com/org/repo/-/issues/503",
            repos=["repo"],
            state=Ticket.State.SHIPPED,
            extra={"mrs": {mr_url: {"draft": False}}},
        )

        self._monkeypatch.setattr(
            "teatree.backends.slack.search_review_permalinks",
            lambda **kw: [
                SlackReviewMatch(
                    mr_url=mr_url,
                    permalink="https://team.slack.com/archives/C123/p170000",
                    channel="review-crew",
                ),
            ],
        )

        with _patch_overlay(self._SLACK_OVERLAY):
            result = SyncResult()
            _fetch_review_permalinks(result)

        assert result.reviews_synced == 1
        ticket = Ticket.objects.get(issue_url="https://gitlab.com/org/repo/-/issues/503")
        mr = ticket.extra["mrs"][mr_url]
        assert mr["review_permalink"] == "https://team.slack.com/archives/C123/p170000"
        assert mr["review_channel"] == "review-crew"

    def test_skips_non_dict_mrs_in_collection(self) -> None:
        """Tickets with non-dict mrs are skipped during collection (line 370)."""
        Ticket.objects.create(
            overlay="test",
            issue_url="https://gitlab.com/org/repo/-/issues/801",
            repos=["repo"],
            state=Ticket.State.SHIPPED,
            extra={"mrs": "not-a-dict"},
        )
        self._monkeypatch.setattr("teatree.backends.slack.search_review_permalinks", lambda **kw: [])

        with _patch_overlay(self._SLACK_OVERLAY):
            result = SyncResult()
            _fetch_review_permalinks(result)
        assert result.reviews_synced == 0

    def test_skips_non_dict_mr_entry_in_collection(self) -> None:
        """Individual non-dict MR entries are skipped during collection (line 373)."""
        Ticket.objects.create(
            overlay="test",
            issue_url="https://gitlab.com/org/repo/-/issues/802",
            repos=["repo"],
            state=Ticket.State.SHIPPED,
            extra={"mrs": {"https://gitlab.com/mr/1": "not-a-dict"}},
        )
        self._monkeypatch.setattr("teatree.backends.slack.search_review_permalinks", lambda **kw: [])

        with _patch_overlay(self._SLACK_OVERLAY):
            result = SyncResult()
            _fetch_review_permalinks(result)
        assert result.reviews_synced == 0


class TestFetchNotionStatuses:
    def test_raises(self) -> None:
        with pytest.raises(NotImplementedError, match="Notion status sync"):
            fetch_notion_statuses()


class TestOverlayName:
    def test_returns_name_for_registered_overlay(self) -> None:
        overlay = SyncOverlay()
        with _patch_overlay(overlay, name="my-overlay"):
            assert _overlay_name(overlay) == "my-overlay"

    def test_returns_empty_for_unknown_overlay(self) -> None:
        overlay = SyncOverlay()
        other = SyncOverlay()
        with _patch_overlay(other, name="other"):
            assert _overlay_name(overlay) == ""


class TestResolveIssueHandles404:
    def test_returns_none_on_404(self) -> None:
        import httpx  # noqa: PLC0415

        client = MagicMock()
        client.resolve_project.return_value = ProjectInfo(
            project_id=1, path_with_namespace="org/repo", short_name="repo"
        )
        client.get_issue.side_effect = httpx.HTTPStatusError(
            "404 Not Found",
            request=MagicMock(),
            response=MagicMock(status_code=404),
        )
        result = _resolve_issue(client, "https://gitlab.com/org/repo/-/issues/123")
        assert result is None

    def test_returns_issue_on_success(self) -> None:
        client = MagicMock()
        client.resolve_project.return_value = ProjectInfo(
            project_id=1, path_with_namespace="org/repo", short_name="repo"
        )
        client.get_issue.return_value = {"id": 123, "title": "Test issue", "labels": []}
        result = _resolve_issue(client, "https://gitlab.com/org/repo/-/issues/123")
        assert result is not None
        assert result[0]["title"] == "Test issue"


class TestDetectE2EEvidence:
    def test_finds_evidence_with_keyword_and_image(self) -> None:
        discussions = [
            {
                "notes": [
                    {
                        "id": 42,
                        "body": "E2E test evidence:\n![screenshot](/uploads/abc.png)",
                    },
                ],
            },
        ]
        url = _detect_e2e_evidence(discussions, "https://gitlab.com/org/repo/-/merge_requests/1")
        assert url == "https://gitlab.com/org/repo/-/merge_requests/1#note_42"

    def test_skips_keyword_without_image(self) -> None:
        discussions = [{"notes": [{"id": 1, "body": "E2E tests look good"}]}]
        assert _detect_e2e_evidence(discussions, "https://example.com") == ""

    def test_skips_image_without_keyword(self) -> None:
        discussions = [{"notes": [{"id": 1, "body": "![logo](/uploads/logo.png)"}]}]
        assert _detect_e2e_evidence(discussions, "https://example.com") == ""

    def test_empty_discussions(self) -> None:
        assert _detect_e2e_evidence([], "https://example.com") == ""

    def test_non_dict_entries_skipped(self) -> None:
        bad_input: list[RawAPIDict] = ["not-a-dict"]  # type: ignore[list-item]
        assert _detect_e2e_evidence(bad_input, "https://example.com") == ""


class TestApplyMergedStatusAllMerged(TestCase):
    @patch("teatree.core.sync.cleanup_worktree")
    def test_advances_state_when_all_merged_no_discussions(self, mock_cleanup: MagicMock) -> None:
        """All MRs merged but none have discussions — state should still advance."""
        ticket = Ticket.objects.create(
            issue_url="https://gitlab.com/org/repo/-/issues/1",
            state=Ticket.State.IN_REVIEW,
            extra={"mrs": {"url1": {"title": "MR1"}, "url2": {"title": "MR2"}}},
        )
        result = SyncResult()
        _apply_merged_status(ticket, {"url1", "url2"}, result)
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.MERGED

    def test_does_not_advance_when_some_unmerged(self) -> None:
        ticket = Ticket.objects.create(
            issue_url="https://gitlab.com/org/repo/-/issues/2",
            state=Ticket.State.IN_REVIEW,
            extra={"mrs": {"url1": {"title": "MR1"}, "url2": {"title": "MR2"}}},
        )
        result = SyncResult()
        _apply_merged_status(ticket, {"url1"}, result)
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.IN_REVIEW

    @patch("teatree.core.sync.cleanup_worktree")
    def test_auto_cleans_worktrees_on_merge(self, mock_cleanup: MagicMock) -> None:
        ticket = Ticket.objects.create(
            issue_url="https://gitlab.com/org/repo/-/issues/3",
            state=Ticket.State.IN_REVIEW,
            extra={"mrs": {"url1": {"title": "MR1"}}},
        )
        Worktree.objects.create(
            overlay="test",
            ticket=ticket,
            repo_path="org/repo",
            branch="fix-3",
        )
        result = SyncResult()
        _apply_merged_status(ticket, {"url1"}, result)
        mock_cleanup.assert_called_once()
        assert result.worktrees_cleaned == 1

    @patch("teatree.core.sync.cleanup_worktree", side_effect=RuntimeError("cleanup failed"))
    def test_cleanup_failure_does_not_block_merge(self, mock_cleanup: MagicMock) -> None:
        ticket = Ticket.objects.create(
            issue_url="https://gitlab.com/org/repo/-/issues/4",
            state=Ticket.State.IN_REVIEW,
            extra={"mrs": {"url1": {"title": "MR1"}}},
        )
        Worktree.objects.create(
            overlay="test",
            ticket=ticket,
            repo_path="org/repo",
            branch="fix-4",
        )
        result = SyncResult()
        _apply_merged_status(ticket, {"url1"}, result)
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.MERGED
        assert result.worktrees_cleaned == 0
        assert any("cleanup failed" in e for e in result.errors)


class TestSyncFollowup(TestCase):
    _OVERLAY = SyncOverlay()

    @pytest.fixture(autouse=True)
    def _with_overlay(self, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
        self._monkeypatch = monkeypatch
        with _patch_overlay(self._OVERLAY):
            yield

    def test_creates_tickets_from_mrs(self) -> None:
        mock_client = _make_mock_client([_MR_WITH_ISSUE, _MR_WITHOUT_ISSUE])
        self._monkeypatch.setattr("teatree.backends.gitlab_api.GitLabAPI", lambda **_kw: mock_client)

        result = sync_followup()

        assert result.mrs_found == 2
        assert result.tickets_created == 2
        assert result.errors == []
        assert Ticket.objects.count() == 2

        issue_ticket = Ticket.objects.get(issue_url="https://gitlab.com/org/repo/-/issues/100")
        assert "repo" in issue_ticket.repos
        assert "mrs" in issue_ticket.extra
        # Non-draft MR should have pipeline data
        mr_data = issue_ticket.extra["mrs"][_MR_WITH_ISSUE["web_url"]]
        assert mr_data["pipeline_status"] == "success"
        assert mr_data["approvals"] == {"count": 0, "required": 1}

        mr_ticket = Ticket.objects.get(issue_url=_MR_WITHOUT_ISSUE["web_url"])
        assert mr_ticket.extra["mrs"][_MR_WITHOUT_ISSUE["web_url"]]["draft"] is True

    def test_fetches_issue_labels(self) -> None:
        mock_client = _make_mock_client([_MR_WITH_ISSUE])
        self._monkeypatch.setattr("teatree.backends.gitlab_api.GitLabAPI", lambda **_kw: mock_client)

        result = sync_followup()

        assert result.labels_fetched == 1
        ticket = Ticket.objects.get(issue_url="https://gitlab.com/org/repo/-/issues/100")
        assert ticket.extra["tracker_status"] == "Process::Doing"
        assert ticket.extra["issue_title"] == "Issue title"

    def test_updates_existing_ticket(self) -> None:
        Ticket.objects.create(
            overlay="test",
            issue_url="https://gitlab.com/org/repo/-/issues/100",
            repos=["old-repo"],
            extra={"mrs": {}},
        )

        mock_client = _make_mock_client([_MR_WITH_ISSUE])
        self._monkeypatch.setattr("teatree.backends.gitlab_api.GitLabAPI", lambda **_kw: mock_client)

        result = sync_followup()

        assert result.tickets_created == 0
        assert result.tickets_updated == 1
        ticket = Ticket.objects.get(issue_url="https://gitlab.com/org/repo/-/issues/100")
        assert "repo" in ticket.repos
        assert "old-repo" in ticket.repos
        assert _MR_WITH_ISSUE["web_url"] in ticket.extra["mrs"]

    def test_returns_error_when_token_missing(self) -> None:
        overlay = SyncOverlay(gitlab_token="")
        with _patch_overlay(overlay):
            result = sync_followup()

        assert len(result.errors) == 1
        assert "No code host token for" in result.errors[0]

    def test_captures_api_errors(self) -> None:
        mock_client = MagicMock()
        mock_client.list_all_open_mrs.side_effect = RuntimeError("API timeout")
        self._monkeypatch.setattr("teatree.backends.gitlab_api.GitLabAPI", lambda **_kw: mock_client)

        result = sync_followup()

        assert result.mrs_found == 0
        assert len(result.errors) == 1
        assert "API timeout" in result.errors[0]

    def test_returns_error_when_username_missing(self) -> None:
        overlay = SyncOverlay(gitlab_username="")
        mock_client = MagicMock()
        mock_client.current_username.return_value = ""
        self._monkeypatch.setattr("teatree.backends.gitlab_api.GitLabAPI", lambda **_kw: mock_client)

        with _patch_overlay(overlay):
            result = sync_followup()

        assert result.errors == ["GitLab username is not configured in overlay"]

    def test_updates_existing_mr_only_ticket(self) -> None:
        Ticket.objects.create(
            overlay="test",
            issue_url=_MR_WITHOUT_ISSUE["web_url"],
            repos=["repo"],
            extra={"mrs": {}},
        )

        mock_client = _make_mock_client([_MR_WITHOUT_ISSUE])
        self._monkeypatch.setattr("teatree.backends.gitlab_api.GitLabAPI", lambda **_kw: mock_client)

        result = sync_followup()

        assert result.tickets_updated == 1
        ticket = Ticket.objects.get(issue_url=_MR_WITHOUT_ISSUE["web_url"])
        assert _MR_WITHOUT_ISSUE["web_url"] in ticket.extra["mrs"]

    def test_handles_corrupted_extra_field(self) -> None:
        Ticket.objects.create(
            overlay="test",
            issue_url="https://gitlab.com/org/repo/-/issues/100",
            repos=["repo"],
            extra={"mrs": "not-a-dict"},
        )

        mock_client = _make_mock_client([_MR_WITH_ISSUE])
        self._monkeypatch.setattr("teatree.backends.gitlab_api.GitLabAPI", lambda **_kw: mock_client)

        result = sync_followup()

        assert result.tickets_updated == 1
        ticket = Ticket.objects.get(issue_url="https://gitlab.com/org/repo/-/issues/100")
        assert isinstance(ticket.extra["mrs"], dict)

    def test_first_run_passes_no_updated_after(self) -> None:
        """First sync (no cached timestamp) should call list_open_mrs without updated_after."""
        cache.delete(LAST_SYNC_CACHE_KEY)
        mock_client = _make_mock_client([_MR_WITH_ISSUE])
        self._monkeypatch.setattr("teatree.backends.gitlab_api.GitLabAPI", lambda **_kw: mock_client)

        sync_followup()

        mock_client.list_all_open_mrs.assert_called_once_with("testuser", updated_after=None)

    def test_stores_timestamp_and_uses_it_on_next_run(self) -> None:
        """After a successful sync, the timestamp is cached and passed on the next call."""
        cache.delete(LAST_SYNC_CACHE_KEY)
        mock_client = _make_mock_client([_MR_WITH_ISSUE])
        self._monkeypatch.setattr("teatree.backends.gitlab_api.GitLabAPI", lambda **_kw: mock_client)

        # First run: stores the timestamp
        sync_followup()
        stored = cache.get(LAST_SYNC_CACHE_KEY)
        assert stored is not None

        # Second run: should pass the stored timestamp as updated_after
        mock_client.reset_mock()
        mock_client.list_all_open_mrs.return_value = []
        sync_followup()

        mock_client.list_all_open_mrs.assert_called_once_with("testuser", updated_after=stored)

    def test_stores_timestamp_even_when_no_mrs_returned(self) -> None:
        """Timestamp is stored after a successful sync even if zero MRs are returned."""
        cache.delete(LAST_SYNC_CACHE_KEY)
        mock_client = _make_mock_client([])
        self._monkeypatch.setattr("teatree.backends.gitlab_api.GitLabAPI", lambda **_kw: mock_client)

        sync_followup()

        assert cache.get(LAST_SYNC_CACHE_KEY) is not None

    def test_creates_ticket_with_inferred_state(self) -> None:
        """New ticket from a non-draft MR should be SHIPPED, not NOT_STARTED."""
        mock_client = _make_mock_client([_MR_WITH_ISSUE])
        self._monkeypatch.setattr("teatree.backends.gitlab_api.GitLabAPI", lambda **_kw: mock_client)

        sync_followup()

        ticket = Ticket.objects.get(issue_url="https://gitlab.com/org/repo/-/issues/100")
        assert ticket.state == Ticket.State.SHIPPED

    def test_creates_draft_ticket_as_started(self) -> None:
        """New ticket from a draft MR should be STARTED."""
        mock_client = _make_mock_client([_MR_WITHOUT_ISSUE])
        self._monkeypatch.setattr("teatree.backends.gitlab_api.GitLabAPI", lambda **_kw: mock_client)

        sync_followup()

        ticket = Ticket.objects.get(issue_url=_MR_WITHOUT_ISSUE["web_url"])
        assert ticket.state == Ticket.State.STARTED

    def test_advances_existing_ticket_state(self) -> None:
        """Existing ticket at NOT_STARTED should advance when MR data implies a later state."""
        Ticket.objects.create(
            overlay="test",
            issue_url="https://gitlab.com/org/repo/-/issues/100",
            repos=["repo"],
            state=Ticket.State.NOT_STARTED,
            extra={"mrs": {}},
        )
        mock_client = _make_mock_client([_MR_WITH_ISSUE])
        self._monkeypatch.setattr("teatree.backends.gitlab_api.GitLabAPI", lambda **_kw: mock_client)

        sync_followup()

        ticket = Ticket.objects.get(issue_url="https://gitlab.com/org/repo/-/issues/100")
        assert ticket.state == Ticket.State.SHIPPED

    def test_does_not_regress_ticket_state(self) -> None:
        """Ticket already at IN_REVIEW should not regress to SHIPPED on sync."""
        Ticket.objects.create(
            overlay="test",
            issue_url="https://gitlab.com/org/repo/-/issues/100",
            repos=["repo"],
            state=Ticket.State.IN_REVIEW,
            extra={"mrs": {}},
        )
        # MR with no approvals -> inferred SHIPPED, but ticket is already at IN_REVIEW
        mock_client = _make_mock_client([_MR_WITH_ISSUE])
        mock_client.get_mr_approvals.return_value = {"count": 0, "required": 1}
        self._monkeypatch.setattr("teatree.backends.gitlab_api.GitLabAPI", lambda **_kw: mock_client)

        sync_followup()

        ticket = Ticket.objects.get(issue_url="https://gitlab.com/org/repo/-/issues/100")
        assert ticket.state == Ticket.State.IN_REVIEW

    def test_handles_non_list_reviewers(self) -> None:
        """When reviewers is not a list (e.g. None), reviewer fields are omitted."""
        mr = {
            **_MR_WITHOUT_ISSUE,
            "reviewers": None,
        }
        mock_client = _make_mock_client([mr])
        self._monkeypatch.setattr("teatree.backends.gitlab_api.GitLabAPI", lambda **_kw: mock_client)

        result = sync_followup()

        assert result.tickets_created == 1
        ticket = Ticket.objects.get(issue_url=mr["web_url"])
        mr_data = ticket.extra["mrs"][mr["web_url"]]
        assert "review_requested" not in mr_data
        assert "reviewer_names" not in mr_data

    def test_deduplicates_tickets_on_upsert(self) -> None:
        """When duplicate tickets exist for the same issue_url, sync merges and deletes extras."""
        ticket_a = Ticket.objects.create(
            overlay="test",
            issue_url="https://gitlab.com/org/repo/-/issues/100",
            repos=["repo"],
            extra={"mrs": {"https://mr/old": {"title": "old"}}},
            state=Ticket.State.STARTED,
        )
        dup_b = Ticket.objects.create(
            overlay="test",
            issue_url="https://gitlab.com/org/repo/-/issues/101",
            repos=["other-repo"],
            extra={"mrs": {"https://mr/dup": {"title": "dup"}}},
        )
        dup_c = Ticket.objects.create(
            overlay="test",
            issue_url="https://gitlab.com/org/repo/-/issues/102",
            repos=[],
            extra={},
        )

        original_filter = Ticket.objects.filter

        def patched_filter(**kwargs):
            qs = original_filter(**kwargs)
            if kwargs.get("issue_url") == "https://gitlab.com/org/repo/-/issues/100":
                return Ticket.objects.filter(pk__in=[ticket_a.pk, dup_b.pk, dup_c.pk]).order_by("pk")
            return qs

        self._monkeypatch.setattr(Ticket.objects, "filter", patched_filter)

        mock_client = _make_mock_client([_MR_WITH_ISSUE])
        self._monkeypatch.setattr("teatree.backends.gitlab_api.GitLabAPI", lambda **_kw: mock_client)

        result = sync_followup()

        assert result.tickets_updated >= 1
        assert not Ticket.objects.filter(pk=dup_b.pk).exists()
        assert not Ticket.objects.filter(pk=dup_c.pk).exists()
        ticket_a.refresh_from_db()
        assert "https://mr/dup" in ticket_a.extra["mrs"]
        assert "other-repo" in ticket_a.repos


class TestSyncFollowupWorkItems(TestCase):
    _OVERLAY = SyncOverlay()

    @pytest.fixture(autouse=True)
    def _with_overlay(self, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
        self._monkeypatch = monkeypatch
        with _patch_overlay(self._OVERLAY):
            yield

    def test_fetches_work_item_status(self) -> None:
        """Work items without Process:: labels get their status from the GraphQL Status widget."""
        mock_client = _make_mock_client([_MR_WITH_WORK_ITEM])
        mock_client.get_issue.return_value = {"labels": [], "title": "Work item title"}
        mock_client.get_work_item_status.return_value = "In progress"
        self._monkeypatch.setattr("teatree.backends.gitlab_api.GitLabAPI", lambda **_kw: mock_client)

        result = sync_followup()

        assert result.labels_fetched == 1
        ticket = Ticket.objects.get(issue_url="https://gitlab.com/org/repo/-/work_items/200")
        assert ticket.extra["tracker_status"] == "In progress"
        assert ticket.extra["issue_title"] == "Work item title"

    def test_process_label_takes_precedence(self) -> None:
        """When a work item has Process:: labels, those take precedence over the Status widget."""
        mock_client = _make_mock_client([_MR_WITH_WORK_ITEM])
        mock_client.get_issue.return_value = {"labels": ["Process::Doing"], "title": "Work item title"}
        mock_client.get_work_item_status.return_value = "In progress"
        self._monkeypatch.setattr("teatree.backends.gitlab_api.GitLabAPI", lambda **_kw: mock_client)

        result = sync_followup()

        assert result.labels_fetched == 1
        ticket = Ticket.objects.get(issue_url="https://gitlab.com/org/repo/-/work_items/200")
        # Process:: label wins - GraphQL should NOT have been called
        assert ticket.extra["tracker_status"] == "Process::Doing"
        mock_client.get_work_item_status.assert_not_called()

    def test_status_none_falls_through(self) -> None:
        """When GraphQL returns no status, tracker_status stays empty."""
        mock_client = _make_mock_client([_MR_WITH_WORK_ITEM])
        mock_client.get_issue.return_value = {"labels": [], "title": "Work item title"}
        mock_client.get_work_item_status.return_value = None
        self._monkeypatch.setattr("teatree.backends.gitlab_api.GitLabAPI", lambda **_kw: mock_client)

        sync_followup()

        ticket = Ticket.objects.get(issue_url="https://gitlab.com/org/repo/-/work_items/200")
        assert "tracker_status" not in ticket.extra


class TestSyncFollowupMergedMrs(TestCase):
    _OVERLAY = SyncOverlay()

    @pytest.fixture(autouse=True)
    def _with_overlay(self, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
        self._monkeypatch = monkeypatch
        with _patch_overlay(self._OVERLAY):
            yield

    def test_removes_discussions_from_merged_mr(self) -> None:
        """When an MR is merged, its discussions should be removed from the ticket."""
        Ticket.objects.create(
            overlay="test",
            issue_url="https://gitlab.com/org/repo/-/issues/100",
            repos=["repo"],
            state=Ticket.State.IN_REVIEW,
            extra={
                "mrs": {
                    "https://gitlab.com/org/repo/-/merge_requests/42": {
                        "url": "https://gitlab.com/org/repo/-/merge_requests/42",
                        "repo": "repo",
                        "iid": 42,
                        "discussions": [
                            {"status": "addressed", "detail": "Nit: simplify dict comp"},
                            {"status": "addressed", "detail": "import order"},
                        ],
                    },
                },
            },
        )

        mock_client = _make_merged_mock([_MERGED_MR])
        self._monkeypatch.setattr("teatree.backends.gitlab_api.GitLabAPI", lambda **_kw: mock_client)

        result = sync_followup()

        assert result.mrs_merged == 1
        ticket = Ticket.objects.get(issue_url="https://gitlab.com/org/repo/-/issues/100")
        mr = ticket.extra["mrs"]["https://gitlab.com/org/repo/-/merge_requests/42"]
        assert "discussions" not in mr

    def test_advances_ticket_to_merged_when_all_mrs_merged(self) -> None:
        """When all MRs for a ticket are merged, ticket state advances to MERGED."""
        Ticket.objects.create(
            overlay="test",
            issue_url="https://gitlab.com/org/repo/-/issues/100",
            repos=["repo"],
            state=Ticket.State.IN_REVIEW,
            extra={
                "mrs": {
                    "https://gitlab.com/org/repo/-/merge_requests/42": {
                        "url": "https://gitlab.com/org/repo/-/merge_requests/42",
                        "repo": "repo",
                        "iid": 42,
                        "discussions": [{"status": "addressed", "detail": "nit"}],
                    },
                },
            },
        )

        mock_client = _make_merged_mock([_MERGED_MR])
        self._monkeypatch.setattr("teatree.backends.gitlab_api.GitLabAPI", lambda **_kw: mock_client)

        sync_followup()

        ticket = Ticket.objects.get(issue_url="https://gitlab.com/org/repo/-/issues/100")
        assert ticket.state == Ticket.State.MERGED

    def test_does_not_advance_when_some_mrs_still_open(self) -> None:
        """Ticket should stay in current state if only some MRs are merged."""
        Ticket.objects.create(
            overlay="test",
            issue_url="https://gitlab.com/org/repo/-/issues/100",
            repos=["repo"],
            state=Ticket.State.IN_REVIEW,
            extra={
                "mrs": {
                    "https://gitlab.com/org/repo/-/merge_requests/42": {
                        "url": "https://gitlab.com/org/repo/-/merge_requests/42",
                        "repo": "repo",
                        "iid": 42,
                        "discussions": [{"status": "addressed", "detail": "nit"}],
                    },
                    "https://gitlab.com/org/repo/-/merge_requests/99": {
                        "url": "https://gitlab.com/org/repo/-/merge_requests/99",
                        "repo": "repo",
                        "iid": 99,
                        "discussions": [{"status": "needs_reply", "detail": "fix this"}],
                    },
                },
            },
        )

        # Only MR 42 is merged; MR 99 is still open
        mock_client = _make_merged_mock([_MERGED_MR])
        self._monkeypatch.setattr("teatree.backends.gitlab_api.GitLabAPI", lambda **_kw: mock_client)

        sync_followup()

        ticket = Ticket.objects.get(issue_url="https://gitlab.com/org/repo/-/issues/100")
        assert ticket.state == Ticket.State.IN_REVIEW
        # Merged MR's discussions removed, open MR's discussions preserved
        assert "discussions" not in ticket.extra["mrs"]["https://gitlab.com/org/repo/-/merge_requests/42"]
        assert "discussions" in ticket.extra["mrs"]["https://gitlab.com/org/repo/-/merge_requests/99"]

    def test_handles_merged_mr_fetch_failure(self) -> None:
        """When merged MR fetch fails, error is appended but sync continues."""
        mock_client = _make_mock_client([])
        mock_client.list_recently_merged_mrs.side_effect = RuntimeError("timeout")
        self._monkeypatch.setattr("teatree.backends.gitlab_api.GitLabAPI", lambda **_kw: mock_client)

        result = sync_followup()

        assert any("Merged MR fetch failed" in e for e in result.errors)

    def test_skips_ticket_with_no_mrs(self) -> None:
        """Ticket with empty/missing mrs dict should be skipped in merged detection."""
        Ticket.objects.create(
            overlay="test",
            issue_url="https://gitlab.com/org/repo/-/issues/300",
            repos=["repo"],
            state=Ticket.State.IN_REVIEW,
            extra={"mrs": {}},
        )

        mock_client = _make_merged_mock([_MERGED_MR])
        self._monkeypatch.setattr("teatree.backends.gitlab_api.GitLabAPI", lambda **_kw: mock_client)

        result = sync_followup()

        assert result.mrs_merged == 0

    def test_skips_non_dict_mr_entry(self) -> None:
        """Non-dict mr_entry values should be skipped in merged detection."""
        Ticket.objects.create(
            overlay="test",
            issue_url="https://gitlab.com/org/repo/-/issues/301",
            repos=["repo"],
            state=Ticket.State.IN_REVIEW,
            extra={
                "mrs": {
                    "https://gitlab.com/org/repo/-/merge_requests/42": "not-a-dict",
                },
            },
        )

        mock_client = _make_merged_mock([_MERGED_MR])
        self._monkeypatch.setattr("teatree.backends.gitlab_api.GitLabAPI", lambda **_kw: mock_client)

        result = sync_followup()

        # Non-dict entries are skipped; no crash, no merge count
        assert result.mrs_merged == 0

    def test_no_change_when_mr_has_no_discussions(self) -> None:
        """Merged MR without discussions causes no save (no changed flag)."""
        Ticket.objects.create(
            overlay="test",
            issue_url="https://gitlab.com/org/repo/-/issues/302",
            repos=["repo"],
            state=Ticket.State.IN_REVIEW,
            extra={
                "mrs": {
                    "https://gitlab.com/org/repo/-/merge_requests/42": {
                        "url": "https://gitlab.com/org/repo/-/merge_requests/42",
                        "repo": "repo",
                        "iid": 42,
                        # No "discussions" key
                    },
                },
            },
        )

        mock_client = _make_merged_mock([_MERGED_MR])
        self._monkeypatch.setattr("teatree.backends.gitlab_api.GitLabAPI", lambda **_kw: mock_client)

        result = sync_followup()

        # MR is counted as merged even without discussions
        assert result.mrs_merged == 1


class TestSyncFollowupLabels(TestCase):
    _OVERLAY = SyncOverlay()

    @pytest.fixture(autouse=True)
    def _with_overlay(self, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
        self._monkeypatch = monkeypatch
        with _patch_overlay(self._OVERLAY):
            yield

    def test_skips_issue_url_with_no_regex_match(self) -> None:
        """Issue URLs not matching the gitlab pattern are skipped."""
        Ticket.objects.create(
            overlay="test",
            issue_url="https://gitlab.com/weird-url/-/issues/999",
            repos=["repo"],
            state=Ticket.State.STARTED,
            extra={},
        )

        mock_client = _make_mock_client([])
        mock_client.resolve_project.return_value = None  # Force no project
        self._monkeypatch.setattr("teatree.backends.gitlab_api.GitLabAPI", lambda **_kw: mock_client)

        result = sync_followup()

        assert result.labels_fetched == 0

    def test_skips_iid_zero(self) -> None:
        """Issue with iid 0 should be skipped."""
        Ticket.objects.create(
            overlay="test",
            issue_url="https://gitlab.com/org/repo/-/issues/0",
            repos=["repo"],
            state=Ticket.State.STARTED,
            extra={},
        )

        mock_client = _make_mock_client([])
        self._monkeypatch.setattr("teatree.backends.gitlab_api.GitLabAPI", lambda **_kw: mock_client)

        result = sync_followup()

        assert result.labels_fetched == 0

    def test_skips_when_project_not_resolved(self) -> None:
        """When resolve_project returns None, skip the ticket."""
        Ticket.objects.create(
            overlay="test",
            issue_url="https://gitlab.com/org/repo/-/issues/50",
            repos=["repo"],
            state=Ticket.State.STARTED,
            extra={},
        )

        mock_client = _make_mock_client([])
        mock_client.resolve_project.return_value = None
        self._monkeypatch.setattr("teatree.backends.gitlab_api.GitLabAPI", lambda **_kw: mock_client)

        result = sync_followup()

        assert result.labels_fetched == 0

    def test_skips_when_issue_not_found(self) -> None:
        """When get_issue returns None, skip the ticket."""
        Ticket.objects.create(
            overlay="test",
            issue_url="https://gitlab.com/org/repo/-/issues/50",
            repos=["repo"],
            state=Ticket.State.STARTED,
            extra={},
        )

        mock_client = _make_mock_client([])
        mock_client.get_issue.return_value = None
        self._monkeypatch.setattr("teatree.backends.gitlab_api.GitLabAPI", lambda **_kw: mock_client)

        result = sync_followup()

        assert result.labels_fetched == 0

    def test_no_change_when_labels_and_title_unchanged(self) -> None:
        """When tracker_status and issue_title are already the same, no save happens."""
        Ticket.objects.create(
            overlay="test",
            issue_url="https://gitlab.com/org/repo/-/issues/50",
            repos=["repo"],
            state=Ticket.State.STARTED,
            extra={"tracker_status": "Process::Doing", "issue_title": "Issue title"},
        )

        mock_client = _make_mock_client([])
        self._monkeypatch.setattr("teatree.backends.gitlab_api.GitLabAPI", lambda **_kw: mock_client)

        result = sync_followup()

        # No change -> labels_fetched stays at 0
        assert result.labels_fetched == 0

    def test_skips_non_gitlab_url(self) -> None:
        """Issue URL without GitLab /-/ pattern should not match the regex."""
        Ticket.objects.create(
            overlay="test",
            issue_url="https://github.com/org/repo/issues/5",
            repos=["repo"],
            state=Ticket.State.STARTED,
            extra={},
        )

        mock_client = _make_mock_client([])
        self._monkeypatch.setattr("teatree.backends.gitlab_api.GitLabAPI", lambda **_kw: mock_client)

        result = sync_followup()

        assert result.labels_fetched == 0

    def test_updates_ticket_variant_from_issue_labels(self) -> None:
        """_fetch_issue_labels extracts variant from labels and saves to ticket (lines 323-326)."""
        overlay = SyncOverlay(known_variants=["Acme", "BigCorp"])
        Ticket.objects.create(
            overlay="test",
            issue_url="https://gitlab.com/org/repo/-/issues/600",
            repos=["repo"],
            state=Ticket.State.STARTED,
            extra={},
        )

        mock_client = _make_mock_client([])
        mock_client.get_issue.return_value = {"labels": ["acme", "Bug"], "title": "Fix bug"}
        self._monkeypatch.setattr("teatree.backends.gitlab_api.GitLabAPI", lambda **_kw: mock_client)

        with _patch_overlay(overlay):
            sync_followup()

        ticket = Ticket.objects.get(issue_url="https://gitlab.com/org/repo/-/issues/600")
        assert ticket.variant == "Acme"


class TestMergeResults:
    def test_sums_counts_and_concatenates_errors(self) -> None:
        a = SyncResult(mrs_found=3, tickets_created=1, errors=["err-a"])
        b = SyncResult(mrs_found=5, tickets_updated=2, errors=["err-b"])
        merged = _merge_results(a, b)
        assert merged.mrs_found == 8
        assert merged.tickets_created == 1
        assert merged.tickets_updated == 2
        assert merged.errors == ["err-a", "err-b"]

    def test_merges_empty_results(self) -> None:
        merged = _merge_results(SyncResult(), SyncResult())
        assert merged.mrs_found == 0
        assert merged.errors == []


_GITHUB_ITEM_URL = "https://github.com/souliane/teatree/issues/10"


class TestDualSync(TestCase):
    """When both GitHub and GitLab tokens are present, sync_followup runs both."""

    @pytest.fixture(autouse=True)
    def _with_overlay(self, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
        self._monkeypatch = monkeypatch
        overlay = SyncOverlay(
            github_token="gh-token",
            github_owner="org",
            github_project_number=1,
            gitlab_token="gl-token",
        )
        with _patch_overlay(overlay):
            yield

    def test_runs_both_syncs(self) -> None:
        """Both GitHub and GitLab results are merged into one SyncResult."""
        github_item = MagicMock()
        github_item.url = _GITHUB_ITEM_URL
        github_item.title = "Fix issue"
        github_item.status = "In Progress"
        github_item.position = 1
        github_item.labels = []
        github_item.updated_at = "2026-04-01T00:00:00Z"

        self._monkeypatch.setattr(
            "teatree.backends.github.fetch_project_items",
            lambda *_a, **_kw: [github_item],
        )

        mock_client = _make_mock_client([_MR_WITH_ISSUE])
        self._monkeypatch.setattr("teatree.backends.gitlab_api.GitLabAPI", lambda **_kw: mock_client)

        result = sync_followup()

        assert result.mrs_found == 2  # 1 GitHub + 1 GitLab
        assert result.tickets_created == 2
        assert result.errors == []
        assert Ticket.objects.count() == 2

    def test_gitlab_only_when_no_github_token(self) -> None:
        overlay = SyncOverlay(github_token="", gitlab_token="gl-token")
        mock_client = _make_mock_client([_MR_WITHOUT_ISSUE])
        self._monkeypatch.setattr("teatree.backends.gitlab_api.GitLabAPI", lambda **_kw: mock_client)

        with _patch_overlay(overlay):
            result = sync_followup()

        assert result.mrs_found == 1
        assert result.tickets_created == 1

    def test_no_tokens_returns_error(self) -> None:
        overlay = SyncOverlay(github_token="", gitlab_token="")
        with _patch_overlay(overlay):
            result = sync_followup()

        assert len(result.errors) == 1
        assert "No code host token" in result.errors[0]


class TestSyncReviewerMRs(TestCase):
    def test_caches_reviewer_mrs(self) -> None:
        mock_client = MagicMock()
        mock_client.list_open_mrs_as_reviewer.return_value = [
            {
                "web_url": "https://gitlab.com/org/repo/-/merge_requests/99",
                "title": "Fix widget",
                "iid": 99,
                "draft": False,
                "updated_at": "2026-04-09T10:00:00Z",
                "author": {"username": "alice"},
            },
        ]
        result = SyncResult()

        _sync_reviewer_mrs(mock_client, "testuser", result)

        cached = cache.get(PENDING_REVIEWS_CACHE_KEY)
        assert cached is not None
        assert len(cached) == 1
        assert cached[0]["author"] == "alice"
        assert cached[0]["repo"] == "repo"
        assert result.reviews_synced == 1

        cache.delete(PENDING_REVIEWS_CACHE_KEY)

    def test_handles_api_failure_gracefully(self) -> None:
        mock_client = MagicMock()
        mock_client.list_open_mrs_as_reviewer.side_effect = RuntimeError("API down")
        result = SyncResult()

        _sync_reviewer_mrs(mock_client, "testuser", result)

        assert len(result.errors) == 1
        assert "Reviewer MR fetch failed" in result.errors[0]


# ── GitHub sync ──────────────────────────────────────────────────────


class TestSyncGitHub(TestCase):
    def _make_overlay(self, **kwargs: object) -> SyncOverlay:
        return SyncOverlay(
            gitlab_token="",
            gitlab_username="",
            github_token="gh-test-token",
            github_owner="souliane",
            github_project_number=1,
            **kwargs,
        )

    def test_creates_ticket_from_project_item(self) -> None:
        from teatree.backends.github import ProjectItem  # noqa: PLC0415
        from teatree.core.sync import _sync_github  # noqa: PLC0415

        overlay = self._make_overlay()
        item = ProjectItem(
            issue_number=42,
            title="Test issue",
            url="https://github.com/souliane/teatree/issues/42",
            status="In Progress",
            position=1,
            labels=["bug"],
            updated_at="2026-04-01T00:00:00Z",
        )

        with (
            _patch_overlay(overlay),
            patch("teatree.backends.github.fetch_project_items", return_value=[item]),
            patch("teatree.core.sync._sync_github_reviewer_prs"),
        ):
            result = _sync_github(overlay)

        assert result.tickets_created == 1
        assert result.mrs_found == 1
        ticket = Ticket.objects.get(issue_url="https://github.com/souliane/teatree/issues/42")
        assert ticket.state == Ticket.State.STARTED
        assert ticket.extra["issue_title"] == "Test issue"

    def test_updates_existing_ticket(self) -> None:
        from teatree.backends.github import ProjectItem  # noqa: PLC0415
        from teatree.core.sync import _sync_github  # noqa: PLC0415

        overlay = self._make_overlay()
        Ticket.objects.create(
            issue_url="https://github.com/souliane/teatree/issues/43",
            state=Ticket.State.NOT_STARTED,
            extra={"custom_key": "preserved"},
        )

        item = ProjectItem(
            issue_number=43,
            title="Updated issue",
            url="https://github.com/souliane/teatree/issues/43",
            status="Done",
            position=2,
            labels=["enhancement"],
        )

        with (
            _patch_overlay(overlay),
            patch("teatree.backends.github.fetch_project_items", return_value=[item]),
            patch("teatree.core.sync._sync_github_reviewer_prs"),
        ):
            result = _sync_github(overlay)

        assert result.tickets_updated == 1
        ticket = Ticket.objects.get(issue_url="https://github.com/souliane/teatree/issues/43")
        assert ticket.state == Ticket.State.DELIVERED
        assert ticket.extra["custom_key"] == "preserved"
        assert ticket.extra["issue_title"] == "Updated issue"

    def test_returns_error_for_non_overlay(self) -> None:
        from teatree.core.sync import _sync_github  # noqa: PLC0415

        result = _sync_github("not an overlay")
        assert any("Invalid overlay" in e for e in result.errors)

    def test_returns_error_when_config_missing(self) -> None:
        from teatree.core.sync import _sync_github  # noqa: PLC0415

        overlay = SyncOverlay(
            gitlab_token="",
            gitlab_username="",
            github_token="gh-token",
            github_owner="",
            github_project_number=0,
        )

        with _patch_overlay(overlay):
            result = _sync_github(overlay)

        assert any("not configured" in e for e in result.errors)

    def test_handles_fetch_exception(self) -> None:
        from teatree.core.sync import _sync_github  # noqa: PLC0415

        overlay = self._make_overlay()

        with (
            _patch_overlay(overlay),
            patch("teatree.backends.github.fetch_project_items", side_effect=RuntimeError("API error")),
        ):
            result = _sync_github(overlay)

        assert any("fetch failed" in e for e in result.errors)


class TestSyncGitHubReviewerPrs(TestCase):
    def test_caches_reviewer_prs(self) -> None:
        import json  # noqa: PLC0415
        import subprocess  # noqa: PLC0415

        from teatree.core.sync import _sync_github_reviewer_prs  # noqa: PLC0415

        prs = [
            {
                "url": "https://github.com/org/repo/pull/10",
                "title": "Fix bug",
                "repository": {"name": "repo"},
                "number": 10,
                "author": {"login": "alice"},
                "isDraft": False,
                "updatedAt": "2026-04-01T00:00:00Z",
            },
        ]
        mock_run = MagicMock(
            return_value=subprocess.CompletedProcess([], 0, stdout=json.dumps(prs)),
        )
        result = SyncResult()

        with (
            patch("shutil.which", return_value="/usr/bin/gh"),
            patch("subprocess.run", mock_run),
        ):
            _sync_github_reviewer_prs("gh-token", result)

        assert result.reviews_synced == 1
        cached = cache.get(PENDING_REVIEWS_CACHE_KEY)
        assert cached is not None
        assert len(cached) == 1
        assert cached[0]["author"] == "alice"
        cache.delete(PENDING_REVIEWS_CACHE_KEY)

    def test_skips_when_gh_not_found(self) -> None:
        from teatree.core.sync import _sync_github_reviewer_prs  # noqa: PLC0415

        result = SyncResult()
        with patch("shutil.which", return_value=None):
            _sync_github_reviewer_prs("gh-token", result)

        assert result.reviews_synced == 0

    def test_handles_subprocess_exception(self) -> None:
        from teatree.core.sync import _sync_github_reviewer_prs  # noqa: PLC0415

        result = SyncResult()
        with (
            patch("shutil.which", return_value="/usr/bin/gh"),
            patch("subprocess.run", side_effect=OSError("spawn failed")),
        ):
            _sync_github_reviewer_prs("gh-token", result)

        assert any("reviewer PR fetch failed" in e for e in result.errors)

    def test_returns_early_on_nonzero_exit(self) -> None:
        import subprocess  # noqa: PLC0415

        from teatree.core.sync import _sync_github_reviewer_prs  # noqa: PLC0415

        result = SyncResult()
        mock_run = MagicMock(
            return_value=subprocess.CompletedProcess([], 1, stdout=""),
        )
        with (
            patch("shutil.which", return_value="/usr/bin/gh"),
            patch("subprocess.run", mock_run),
        ):
            _sync_github_reviewer_prs("gh-token", result)

        assert result.reviews_synced == 0

    def test_handles_invalid_json(self) -> None:
        import subprocess  # noqa: PLC0415

        from teatree.core.sync import _sync_github_reviewer_prs  # noqa: PLC0415

        result = SyncResult()
        mock_run = MagicMock(
            return_value=subprocess.CompletedProcess([], 0, stdout="not json"),
        )
        with (
            patch("shutil.which", return_value="/usr/bin/gh"),
            patch("subprocess.run", mock_run),
        ):
            _sync_github_reviewer_prs("gh-token", result)

        assert result.reviews_synced == 0
