from collections.abc import Iterator
from unittest.mock import MagicMock

import pytest
from django.core.cache import cache
from django.test import override_settings

from teetree.core.models import Ticket
from teetree.core.overlay_loader import reset_overlay_cache
from teetree.core.sync import (
    LAST_SYNC_CACHE_KEY,
    SyncResult,
    _extract_issue_url,
    _extract_variant,
    _fetch_review_permalinks,
    _infer_state_from_mrs,
    _merge_ticket_extras,
    _update_ticket,
    sync_followup,
)
from teetree.utils.gitlab_api import ProjectInfo


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


@override_settings(
    TEATREE_OVERLAY_CLASS="tests.teetree_core.conftest.CommandOverlay",
    TEATREE_GITLAB_TOKEN="test-token",
    TEATREE_GITLAB_USERNAME="testuser",
)
@pytest.mark.django_db
def test_sync_creates_tickets_from_mrs(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_client = _make_mock_client([_MR_WITH_ISSUE, _MR_WITHOUT_ISSUE])
    monkeypatch.setattr("teetree.core.sync.GitLabAPI", lambda **_kw: mock_client)

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


@override_settings(
    TEATREE_OVERLAY_CLASS="tests.teetree_core.conftest.CommandOverlay",
    TEATREE_GITLAB_TOKEN="test-token",
    TEATREE_GITLAB_USERNAME="testuser",
)
@pytest.mark.django_db
def test_sync_fetches_issue_labels(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_client = _make_mock_client([_MR_WITH_ISSUE])
    monkeypatch.setattr("teetree.core.sync.GitLabAPI", lambda **_kw: mock_client)

    result = sync_followup()

    assert result.labels_fetched == 1
    ticket = Ticket.objects.get(issue_url="https://gitlab.com/org/repo/-/issues/100")
    assert ticket.extra["tracker_status"] == "Process::Doing"
    assert ticket.extra["issue_title"] == "Issue title"


@override_settings(
    TEATREE_OVERLAY_CLASS="tests.teetree_core.conftest.CommandOverlay",
    TEATREE_GITLAB_TOKEN="test-token",
    TEATREE_GITLAB_USERNAME="testuser",
)
@pytest.mark.django_db
def test_sync_updates_existing_ticket(monkeypatch: pytest.MonkeyPatch) -> None:
    Ticket.objects.create(
        issue_url="https://gitlab.com/org/repo/-/issues/100",
        repos=["old-repo"],
        extra={"mrs": {}},
    )

    mock_client = _make_mock_client([_MR_WITH_ISSUE])
    monkeypatch.setattr("teetree.core.sync.GitLabAPI", lambda **_kw: mock_client)

    result = sync_followup()

    assert result.tickets_created == 0
    assert result.tickets_updated == 1
    ticket = Ticket.objects.get(issue_url="https://gitlab.com/org/repo/-/issues/100")
    assert "repo" in ticket.repos
    assert "old-repo" in ticket.repos
    assert _MR_WITH_ISSUE["web_url"] in ticket.extra["mrs"]


@override_settings(
    TEATREE_OVERLAY_CLASS="tests.teetree_core.conftest.CommandOverlay",
    TEATREE_GITLAB_TOKEN="",
    TEATREE_GITLAB_USERNAME="testuser",
)
def test_sync_returns_error_when_token_missing(monkeypatch: pytest.MonkeyPatch) -> None:

    result = sync_followup()

    assert result.errors == ["TEATREE_GITLAB_TOKEN is not set"]


@override_settings(
    TEATREE_OVERLAY_CLASS="tests.teetree_core.conftest.CommandOverlay",
    TEATREE_GITLAB_TOKEN="test-token",
    TEATREE_GITLAB_USERNAME="testuser",
)
@pytest.mark.django_db
def test_sync_captures_api_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_client = MagicMock()
    mock_client.list_all_open_mrs.side_effect = RuntimeError("API timeout")
    monkeypatch.setattr("teetree.core.sync.GitLabAPI", lambda **_kw: mock_client)

    result = sync_followup()

    assert result.mrs_found == 0
    assert len(result.errors) == 1
    assert "API timeout" in result.errors[0]


@override_settings(
    TEATREE_OVERLAY_CLASS="tests.teetree_core.conftest.CommandOverlay",
    TEATREE_GITLAB_TOKEN="test-token",
    TEATREE_GITLAB_USERNAME="",
)
def test_sync_returns_error_when_username_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_client = MagicMock()
    mock_client.current_username.return_value = ""
    monkeypatch.setattr("teetree.core.sync.GitLabAPI", lambda **_kw: mock_client)

    result = sync_followup()

    assert result.errors == ["TEATREE_GITLAB_USERNAME is not set"]


@override_settings(
    TEATREE_OVERLAY_CLASS="tests.teetree_core.conftest.CommandOverlay",
    TEATREE_GITLAB_TOKEN="test-token",
    TEATREE_GITLAB_USERNAME="testuser",
)
@pytest.mark.django_db
def test_sync_updates_existing_mr_only_ticket(monkeypatch: pytest.MonkeyPatch) -> None:
    Ticket.objects.create(
        issue_url=_MR_WITHOUT_ISSUE["web_url"],
        repos=["repo"],
        extra={"mrs": {}},
    )

    mock_client = _make_mock_client([_MR_WITHOUT_ISSUE])
    monkeypatch.setattr("teetree.core.sync.GitLabAPI", lambda **_kw: mock_client)

    result = sync_followup()

    assert result.tickets_updated == 1
    ticket = Ticket.objects.get(issue_url=_MR_WITHOUT_ISSUE["web_url"])
    assert _MR_WITHOUT_ISSUE["web_url"] in ticket.extra["mrs"]


@override_settings(
    TEATREE_OVERLAY_CLASS="tests.teetree_core.conftest.CommandOverlay",
    TEATREE_GITLAB_TOKEN="test-token",
    TEATREE_GITLAB_USERNAME="testuser",
)
@pytest.mark.django_db
def test_sync_handles_corrupted_extra_field(monkeypatch: pytest.MonkeyPatch) -> None:
    Ticket.objects.create(
        issue_url="https://gitlab.com/org/repo/-/issues/100",
        repos=["repo"],
        extra={"mrs": "not-a-dict"},
    )

    mock_client = _make_mock_client([_MR_WITH_ISSUE])
    monkeypatch.setattr("teetree.core.sync.GitLabAPI", lambda **_kw: mock_client)

    result = sync_followup()

    assert result.tickets_updated == 1
    ticket = Ticket.objects.get(issue_url="https://gitlab.com/org/repo/-/issues/100")
    assert isinstance(ticket.extra["mrs"], dict)


def test_extract_issue_url_from_description() -> None:
    assert _extract_issue_url(_MR_WITH_ISSUE) == "https://gitlab.com/org/repo/-/issues/100"


def test_extract_issue_url_returns_empty_when_none() -> None:
    assert _extract_issue_url(_MR_WITHOUT_ISSUE) == ""


def test_sync_result_defaults() -> None:
    result = SyncResult()
    assert result.labels_fetched == 0
    assert result.errors == []


@override_settings(
    TEATREE_OVERLAY_CLASS="tests.teetree_core.conftest.CommandOverlay",
    TEATREE_GITLAB_TOKEN="test-token",
    TEATREE_GITLAB_USERNAME="testuser",
)
@pytest.mark.django_db
def test_sync_first_run_passes_no_updated_after(monkeypatch: pytest.MonkeyPatch) -> None:
    """First sync (no cached timestamp) should call list_open_mrs without updated_after."""
    cache.delete(LAST_SYNC_CACHE_KEY)
    mock_client = _make_mock_client([_MR_WITH_ISSUE])
    monkeypatch.setattr("teetree.core.sync.GitLabAPI", lambda **_kw: mock_client)

    sync_followup()

    mock_client.list_all_open_mrs.assert_called_once_with("testuser", updated_after=None)


@override_settings(
    TEATREE_OVERLAY_CLASS="tests.teetree_core.conftest.CommandOverlay",
    TEATREE_GITLAB_TOKEN="test-token",
    TEATREE_GITLAB_USERNAME="testuser",
)
@pytest.mark.django_db
def test_sync_stores_timestamp_and_uses_it_on_next_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """After a successful sync, the timestamp is cached and passed on the next call."""
    cache.delete(LAST_SYNC_CACHE_KEY)
    mock_client = _make_mock_client([_MR_WITH_ISSUE])
    monkeypatch.setattr("teetree.core.sync.GitLabAPI", lambda **_kw: mock_client)

    # First run: stores the timestamp
    sync_followup()
    stored = cache.get(LAST_SYNC_CACHE_KEY)
    assert stored is not None

    # Second run: should pass the stored timestamp as updated_after
    mock_client.reset_mock()
    mock_client.list_all_open_mrs.return_value = []
    sync_followup()

    mock_client.list_all_open_mrs.assert_called_once_with("testuser", updated_after=stored)


@override_settings(
    TEATREE_OVERLAY_CLASS="tests.teetree_core.conftest.CommandOverlay",
    TEATREE_GITLAB_TOKEN="test-token",
    TEATREE_GITLAB_USERNAME="testuser",
)
@pytest.mark.django_db
def test_sync_stores_timestamp_even_when_no_mrs_returned(monkeypatch: pytest.MonkeyPatch) -> None:
    """Timestamp is stored after a successful sync even if zero MRs are returned."""
    cache.delete(LAST_SYNC_CACHE_KEY)
    mock_client = _make_mock_client([])
    monkeypatch.setattr("teetree.core.sync.GitLabAPI", lambda **_kw: mock_client)

    sync_followup()

    assert cache.get(LAST_SYNC_CACHE_KEY) is not None


@pytest.mark.django_db
def test_update_ticket_preserves_skill_written_fields() -> None:
    """Skill-written fields (review_channel, review_permalink, e2e_test_plan_url) survive sync updates."""
    ticket = Ticket.objects.create(
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
    new_mr_entry: dict[str, object] = {
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


# --- Work item status sync ---

_MR_WITH_WORK_ITEM = {
    "web_url": "https://gitlab.com/org/repo/-/merge_requests/44",
    "title": "feat: work item feature",
    "description": "feat: work item feature (https://gitlab.com/org/repo/-/work_items/200)\n\nBody",
    "source_branch": "feat/work-item",
    "draft": False,
    "iid": 44,
    "project_id": 123,
}


@override_settings(
    TEATREE_OVERLAY_CLASS="tests.teetree_core.conftest.CommandOverlay",
    TEATREE_GITLAB_TOKEN="test-token",
    TEATREE_GITLAB_USERNAME="testuser",
)
@pytest.mark.django_db
def test_sync_fetches_work_item_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """Work items without Process:: labels get their status from the GraphQL Status widget."""
    mock_client = _make_mock_client([_MR_WITH_WORK_ITEM])
    mock_client.get_issue.return_value = {"labels": [], "title": "Work item title"}
    mock_client.get_work_item_status.return_value = "In progress"
    monkeypatch.setattr("teetree.core.sync.GitLabAPI", lambda **_kw: mock_client)

    result = sync_followup()

    assert result.labels_fetched == 1
    ticket = Ticket.objects.get(issue_url="https://gitlab.com/org/repo/-/work_items/200")
    assert ticket.extra["tracker_status"] == "In progress"
    assert ticket.extra["issue_title"] == "Work item title"


@override_settings(
    TEATREE_OVERLAY_CLASS="tests.teetree_core.conftest.CommandOverlay",
    TEATREE_GITLAB_TOKEN="test-token",
    TEATREE_GITLAB_USERNAME="testuser",
)
@pytest.mark.django_db
def test_sync_work_item_process_label_takes_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    """When a work item has Process:: labels, those take precedence over the Status widget."""
    mock_client = _make_mock_client([_MR_WITH_WORK_ITEM])
    mock_client.get_issue.return_value = {"labels": ["Process::Doing"], "title": "Work item title"}
    mock_client.get_work_item_status.return_value = "In progress"
    monkeypatch.setattr("teetree.core.sync.GitLabAPI", lambda **_kw: mock_client)

    result = sync_followup()

    assert result.labels_fetched == 1
    ticket = Ticket.objects.get(issue_url="https://gitlab.com/org/repo/-/work_items/200")
    # Process:: label wins - GraphQL should NOT have been called
    assert ticket.extra["tracker_status"] == "Process::Doing"
    mock_client.get_work_item_status.assert_not_called()


@override_settings(
    TEATREE_OVERLAY_CLASS="tests.teetree_core.conftest.CommandOverlay",
    TEATREE_GITLAB_TOKEN="test-token",
    TEATREE_GITLAB_USERNAME="testuser",
)
@pytest.mark.django_db
def test_sync_work_item_status_none_falls_through(monkeypatch: pytest.MonkeyPatch) -> None:
    """When GraphQL returns no status, tracker_status stays empty."""
    mock_client = _make_mock_client([_MR_WITH_WORK_ITEM])
    mock_client.get_issue.return_value = {"labels": [], "title": "Work item title"}
    mock_client.get_work_item_status.return_value = None
    monkeypatch.setattr("teetree.core.sync.GitLabAPI", lambda **_kw: mock_client)

    sync_followup()

    ticket = Ticket.objects.get(issue_url="https://gitlab.com/org/repo/-/work_items/200")
    assert "tracker_status" not in ticket.extra


# --- State inference from MR data ---


def test_infer_state_empty_mrs() -> None:
    assert _infer_state_from_mrs({}) == Ticket.State.NOT_STARTED


def test_infer_state_corrupted_mrs() -> None:
    assert _infer_state_from_mrs({"x": "not-a-dict"}) == Ticket.State.NOT_STARTED


def test_infer_state_draft_mr() -> None:
    mrs = {"url1": {"draft": True}}
    assert _infer_state_from_mrs(mrs) == Ticket.State.STARTED


def test_infer_state_non_draft_mr() -> None:
    mrs = {"url1": {"draft": False}}
    assert _infer_state_from_mrs(mrs) == Ticket.State.SHIPPED


def test_infer_state_mr_with_approvals() -> None:
    mrs = {"url1": {"draft": False, "approvals": {"count": 1, "required": 1}}}
    assert _infer_state_from_mrs(mrs) == Ticket.State.IN_REVIEW


def test_infer_state_mr_with_review_requested() -> None:
    mrs = {"url1": {"draft": False, "review_requested": True}}
    assert _infer_state_from_mrs(mrs) == Ticket.State.IN_REVIEW


def test_infer_state_picks_highest_across_mrs() -> None:
    mrs = {
        "url1": {"draft": True},  # STARTED
        "url2": {"draft": False, "approvals": {"count": 1, "required": 1}},  # IN_REVIEW
    }
    assert _infer_state_from_mrs(mrs) == Ticket.State.IN_REVIEW


@override_settings(
    TEATREE_OVERLAY_CLASS="tests.teetree_core.conftest.CommandOverlay",
    TEATREE_GITLAB_TOKEN="test-token",
    TEATREE_GITLAB_USERNAME="testuser",
)
@pytest.mark.django_db
def test_sync_creates_ticket_with_inferred_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """New ticket from a non-draft MR should be SHIPPED, not NOT_STARTED."""
    mock_client = _make_mock_client([_MR_WITH_ISSUE])
    monkeypatch.setattr("teetree.core.sync.GitLabAPI", lambda **_kw: mock_client)

    sync_followup()

    ticket = Ticket.objects.get(issue_url="https://gitlab.com/org/repo/-/issues/100")
    assert ticket.state == Ticket.State.SHIPPED


@override_settings(
    TEATREE_OVERLAY_CLASS="tests.teetree_core.conftest.CommandOverlay",
    TEATREE_GITLAB_TOKEN="test-token",
    TEATREE_GITLAB_USERNAME="testuser",
)
@pytest.mark.django_db
def test_sync_creates_draft_ticket_as_started(monkeypatch: pytest.MonkeyPatch) -> None:
    """New ticket from a draft MR should be STARTED."""
    mock_client = _make_mock_client([_MR_WITHOUT_ISSUE])
    monkeypatch.setattr("teetree.core.sync.GitLabAPI", lambda **_kw: mock_client)

    sync_followup()

    ticket = Ticket.objects.get(issue_url=_MR_WITHOUT_ISSUE["web_url"])
    assert ticket.state == Ticket.State.STARTED


@override_settings(
    TEATREE_OVERLAY_CLASS="tests.teetree_core.conftest.CommandOverlay",
    TEATREE_GITLAB_TOKEN="test-token",
    TEATREE_GITLAB_USERNAME="testuser",
)
@pytest.mark.django_db
def test_sync_advances_existing_ticket_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Existing ticket at NOT_STARTED should advance when MR data implies a later state."""
    Ticket.objects.create(
        issue_url="https://gitlab.com/org/repo/-/issues/100",
        repos=["repo"],
        state=Ticket.State.NOT_STARTED,
        extra={"mrs": {}},
    )
    mock_client = _make_mock_client([_MR_WITH_ISSUE])
    monkeypatch.setattr("teetree.core.sync.GitLabAPI", lambda **_kw: mock_client)

    sync_followup()

    ticket = Ticket.objects.get(issue_url="https://gitlab.com/org/repo/-/issues/100")
    assert ticket.state == Ticket.State.SHIPPED


@override_settings(
    TEATREE_OVERLAY_CLASS="tests.teetree_core.conftest.CommandOverlay",
    TEATREE_GITLAB_TOKEN="test-token",
    TEATREE_GITLAB_USERNAME="testuser",
)
@pytest.mark.django_db
def test_sync_does_not_regress_ticket_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ticket already at IN_REVIEW should not regress to SHIPPED on sync."""
    Ticket.objects.create(
        issue_url="https://gitlab.com/org/repo/-/issues/100",
        repos=["repo"],
        state=Ticket.State.IN_REVIEW,
        extra={"mrs": {}},
    )
    # MR with no approvals → inferred SHIPPED, but ticket is already at IN_REVIEW
    mock_client = _make_mock_client([_MR_WITH_ISSUE])
    mock_client.get_mr_approvals.return_value = {"count": 0, "required": 1}
    monkeypatch.setattr("teetree.core.sync.GitLabAPI", lambda **_kw: mock_client)

    sync_followup()

    ticket = Ticket.objects.get(issue_url="https://gitlab.com/org/repo/-/issues/100")
    assert ticket.state == Ticket.State.IN_REVIEW


# --- Merged MR detection ---

_MERGED_MR = {
    "web_url": "https://gitlab.com/org/repo/-/merge_requests/42",
    "iid": 42,
    "project_id": 123,
}


def _make_merged_mock(merged_mrs: list[dict]) -> MagicMock:
    """Mock client with no open MRs and some merged MRs."""
    mock = _make_mock_client([])
    mock.list_recently_merged_mrs.return_value = merged_mrs
    return mock


@override_settings(
    TEATREE_GITLAB_TOKEN="test-token",
    TEATREE_GITLAB_USERNAME="testuser",
)
@pytest.mark.django_db
def test_sync_removes_discussions_from_merged_mr(monkeypatch: pytest.MonkeyPatch) -> None:
    """When an MR is merged, its discussions should be removed from the ticket."""
    Ticket.objects.create(
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
    monkeypatch.setattr("teetree.core.sync.GitLabAPI", lambda **_kw: mock_client)

    result = sync_followup()

    assert result.mrs_merged == 1
    ticket = Ticket.objects.get(issue_url="https://gitlab.com/org/repo/-/issues/100")
    mr = ticket.extra["mrs"]["https://gitlab.com/org/repo/-/merge_requests/42"]
    assert "discussions" not in mr


@override_settings(
    TEATREE_GITLAB_TOKEN="test-token",
    TEATREE_GITLAB_USERNAME="testuser",
)
@pytest.mark.django_db
def test_sync_advances_ticket_to_merged_when_all_mrs_merged(monkeypatch: pytest.MonkeyPatch) -> None:
    """When all MRs for a ticket are merged, ticket state advances to MERGED."""
    Ticket.objects.create(
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
    monkeypatch.setattr("teetree.core.sync.GitLabAPI", lambda **_kw: mock_client)

    sync_followup()

    ticket = Ticket.objects.get(issue_url="https://gitlab.com/org/repo/-/issues/100")
    assert ticket.state == Ticket.State.MERGED


@override_settings(
    TEATREE_GITLAB_TOKEN="test-token",
    TEATREE_GITLAB_USERNAME="testuser",
)
@pytest.mark.django_db
def test_sync_does_not_advance_to_merged_when_some_mrs_still_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ticket should stay in current state if only some MRs are merged."""
    Ticket.objects.create(
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
    monkeypatch.setattr("teetree.core.sync.GitLabAPI", lambda **_kw: mock_client)

    sync_followup()

    ticket = Ticket.objects.get(issue_url="https://gitlab.com/org/repo/-/issues/100")
    assert ticket.state == Ticket.State.IN_REVIEW
    # Merged MR's discussions removed, open MR's discussions preserved
    assert "discussions" not in ticket.extra["mrs"]["https://gitlab.com/org/repo/-/merge_requests/42"]
    assert "discussions" in ticket.extra["mrs"]["https://gitlab.com/org/repo/-/merge_requests/99"]


# --- _classify_discussions ---

from teetree.core.sync import _classify_discussions, fetch_notion_statuses  # noqa: E402


def test_classify_discussions_skips_non_dict_entries() -> None:
    result = _classify_discussions(["not-a-dict", 42], "me")
    assert result == []


def test_classify_discussions_skips_individual_notes() -> None:
    result = _classify_discussions([{"individual_note": True, "notes": [{"body": "x"}]}], "me")
    assert result == []


def test_classify_discussions_skips_empty_notes() -> None:
    result = _classify_discussions([{"notes": []}], "me")
    assert result == []


def test_classify_discussions_skips_non_list_notes() -> None:
    result = _classify_discussions([{"notes": "not-a-list"}], "me")
    assert result == []


def test_classify_discussions_addressed_when_all_resolved() -> None:
    discussions = [
        {
            "notes": [
                {"body": "Fix this", "resolvable": True, "resolved": True, "author": {"username": "reviewer"}},
            ],
        }
    ]
    result = _classify_discussions(discussions, "me")
    assert len(result) == 1
    assert result[0]["status"] == "addressed"
    assert result[0]["detail"] == "Fix this"


def test_classify_discussions_waiting_reviewer_when_last_author_is_mr_author() -> None:
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
    assert result[0]["status"] == "waiting_reviewer"


def test_classify_discussions_needs_reply_when_last_author_is_not_mr_author() -> None:
    discussions = [
        {
            "notes": [
                {"body": "Please fix", "resolvable": True, "resolved": False, "author": {"username": "reviewer"}},
            ],
        }
    ]
    result = _classify_discussions(discussions, "me")
    assert len(result) == 1
    assert result[0]["status"] == "needs_reply"


def test_classify_discussions_non_dict_last_note_author() -> None:
    """When the last note is not a dict, the author should be empty → needs_reply."""
    discussions = [
        {
            "notes": [
                {"body": "First note", "resolvable": True, "resolved": False, "author": {"username": "reviewer"}},
                "not-a-dict",
            ],
        }
    ]
    result = _classify_discussions(discussions, "me")
    assert result[0]["status"] == "needs_reply"


def test_classify_discussions_non_dict_first_note_body() -> None:
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
    assert result[0]["detail"] == ""  # first_body from non-dict is ""


# --- _detect_merged_mrs edge cases ---


@override_settings(
    TEATREE_GITLAB_TOKEN="test-token",
    TEATREE_GITLAB_USERNAME="testuser",
)
@pytest.mark.django_db
def test_sync_handles_merged_mr_fetch_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """When merged MR fetch fails, error is appended but sync continues."""
    mock_client = _make_mock_client([])
    mock_client.list_recently_merged_mrs.side_effect = RuntimeError("timeout")
    monkeypatch.setattr("teetree.core.sync.GitLabAPI", lambda **_kw: mock_client)

    result = sync_followup()

    assert any("Merged MR fetch failed" in e for e in result.errors)


@override_settings(
    TEATREE_GITLAB_TOKEN="test-token",
    TEATREE_GITLAB_USERNAME="testuser",
)
@pytest.mark.django_db
def test_detect_merged_skips_ticket_with_no_mrs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ticket with empty/missing mrs dict should be skipped in merged detection."""
    Ticket.objects.create(
        issue_url="https://gitlab.com/org/repo/-/issues/300",
        repos=["repo"],
        state=Ticket.State.IN_REVIEW,
        extra={"mrs": {}},
    )

    mock_client = _make_merged_mock([_MERGED_MR])
    monkeypatch.setattr("teetree.core.sync.GitLabAPI", lambda **_kw: mock_client)

    result = sync_followup()

    assert result.mrs_merged == 0


@override_settings(
    TEATREE_GITLAB_TOKEN="test-token",
    TEATREE_GITLAB_USERNAME="testuser",
)
@pytest.mark.django_db
def test_detect_merged_skips_non_dict_mr_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-dict mr_entry values should be skipped in merged detection."""
    Ticket.objects.create(
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
    monkeypatch.setattr("teetree.core.sync.GitLabAPI", lambda **_kw: mock_client)

    result = sync_followup()

    # Non-dict entries are skipped; no crash, no merge count
    assert result.mrs_merged == 0


@override_settings(
    TEATREE_GITLAB_TOKEN="test-token",
    TEATREE_GITLAB_USERNAME="testuser",
)
@pytest.mark.django_db
def test_detect_merged_no_change_when_mr_has_no_discussions(monkeypatch: pytest.MonkeyPatch) -> None:
    """Merged MR without discussions causes no save (no changed flag)."""
    Ticket.objects.create(
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
    monkeypatch.setattr("teetree.core.sync.GitLabAPI", lambda **_kw: mock_client)

    result = sync_followup()

    # MR is counted as merged even without discussions
    assert result.mrs_merged == 1


# --- _fetch_issue_labels edge cases ---


@override_settings(
    TEATREE_GITLAB_TOKEN="test-token",
    TEATREE_GITLAB_USERNAME="testuser",
)
@pytest.mark.django_db
def test_fetch_labels_skips_issue_url_with_no_regex_match(monkeypatch: pytest.MonkeyPatch) -> None:
    """Issue URLs not matching the gitlab pattern are skipped."""
    Ticket.objects.create(
        issue_url="https://gitlab.com/weird-url/-/issues/999",
        repos=["repo"],
        state=Ticket.State.STARTED,
        extra={},
    )

    mock_client = _make_mock_client([])
    mock_client.resolve_project.return_value = None  # Force no project
    monkeypatch.setattr("teetree.core.sync.GitLabAPI", lambda **_kw: mock_client)

    result = sync_followup()

    assert result.labels_fetched == 0


@override_settings(
    TEATREE_GITLAB_TOKEN="test-token",
    TEATREE_GITLAB_USERNAME="testuser",
)
@pytest.mark.django_db
def test_fetch_labels_skips_iid_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """Issue with iid 0 should be skipped."""
    Ticket.objects.create(
        issue_url="https://gitlab.com/org/repo/-/issues/0",
        repos=["repo"],
        state=Ticket.State.STARTED,
        extra={},
    )

    mock_client = _make_mock_client([])
    monkeypatch.setattr("teetree.core.sync.GitLabAPI", lambda **_kw: mock_client)

    result = sync_followup()

    assert result.labels_fetched == 0


@override_settings(
    TEATREE_GITLAB_TOKEN="test-token",
    TEATREE_GITLAB_USERNAME="testuser",
)
@pytest.mark.django_db
def test_fetch_labels_skips_when_project_not_resolved(monkeypatch: pytest.MonkeyPatch) -> None:
    """When resolve_project returns None, skip the ticket."""
    Ticket.objects.create(
        issue_url="https://gitlab.com/org/repo/-/issues/50",
        repos=["repo"],
        state=Ticket.State.STARTED,
        extra={},
    )

    mock_client = _make_mock_client([])
    mock_client.resolve_project.return_value = None
    monkeypatch.setattr("teetree.core.sync.GitLabAPI", lambda **_kw: mock_client)

    result = sync_followup()

    assert result.labels_fetched == 0


@override_settings(
    TEATREE_GITLAB_TOKEN="test-token",
    TEATREE_GITLAB_USERNAME="testuser",
)
@pytest.mark.django_db
def test_fetch_labels_skips_when_issue_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """When get_issue returns None, skip the ticket."""
    Ticket.objects.create(
        issue_url="https://gitlab.com/org/repo/-/issues/50",
        repos=["repo"],
        state=Ticket.State.STARTED,
        extra={},
    )

    mock_client = _make_mock_client([])
    mock_client.get_issue.return_value = None
    monkeypatch.setattr("teetree.core.sync.GitLabAPI", lambda **_kw: mock_client)

    result = sync_followup()

    assert result.labels_fetched == 0


# --- fetch_notion_statuses ---


def test_fetch_notion_statuses_raises() -> None:
    with pytest.raises(NotImplementedError, match="Notion status sync"):
        fetch_notion_statuses()


# --- Reviewer info branch coverage ---


@override_settings(
    TEATREE_OVERLAY_CLASS="tests.teetree_core.conftest.CommandOverlay",
    TEATREE_GITLAB_TOKEN="test-token",
    TEATREE_GITLAB_USERNAME="testuser",
)
@pytest.mark.django_db
def test_sync_handles_non_list_reviewers(monkeypatch: pytest.MonkeyPatch) -> None:
    """When reviewers is not a list (e.g. None), reviewer fields are omitted."""
    mr = {
        **_MR_WITHOUT_ISSUE,
        "reviewers": None,
    }
    mock_client = _make_mock_client([mr])
    monkeypatch.setattr("teetree.core.sync.GitLabAPI", lambda **_kw: mock_client)

    result = sync_followup()

    assert result.tickets_created == 1
    ticket = Ticket.objects.get(issue_url=mr["web_url"])
    mr_data = ticket.extra["mrs"][mr["web_url"]]
    assert "review_requested" not in mr_data
    assert "reviewer_names" not in mr_data


# --- _process_label branch: empty labels ---


@override_settings(
    TEATREE_GITLAB_TOKEN="test-token",
    TEATREE_GITLAB_USERNAME="testuser",
)
@pytest.mark.django_db
def test_fetch_labels_no_change_when_labels_and_title_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """When tracker_status and issue_title are already the same, no save happens."""
    Ticket.objects.create(
        issue_url="https://gitlab.com/org/repo/-/issues/50",
        repos=["repo"],
        state=Ticket.State.STARTED,
        extra={"tracker_status": "Process::Doing", "issue_title": "Issue title"},
    )

    mock_client = _make_mock_client([])
    monkeypatch.setattr("teetree.core.sync.GitLabAPI", lambda **_kw: mock_client)

    result = sync_followup()

    # No change → labels_fetched stays at 0
    assert result.labels_fetched == 0


@override_settings(
    TEATREE_GITLAB_TOKEN="test-token",
    TEATREE_GITLAB_USERNAME="testuser",
)
@pytest.mark.django_db
def test_fetch_labels_skips_non_gitlab_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """Issue URL not containing gitlab.com should not match the Python regex (line 278)."""
    Ticket.objects.create(
        issue_url="https://example.com/org/repo/-/issues/5",
        repos=["repo"],
        state=Ticket.State.STARTED,
        extra={},
    )

    mock_client = _make_mock_client([])
    monkeypatch.setattr("teetree.core.sync.GitLabAPI", lambda **_kw: mock_client)

    result = sync_followup()

    assert result.labels_fetched == 0


# --- _process_label: non-process label ---

from teetree.core.sync import _process_label  # noqa: E402


def test_process_label_returns_none_for_non_process_labels() -> None:
    """Labels without Process:: prefix should yield None."""
    assert _process_label(["Priority::High", "Bug"]) is None


def test_process_label_returns_none_for_empty_labels() -> None:
    assert _process_label([]) is None


# --- _infer_state_from_mrs: second MR doesn't advance ---


def test_infer_state_second_mr_does_not_advance_when_lower() -> None:
    """When second MR infers a lower state than the first, best stays unchanged."""
    mrs = {
        "url1": {"draft": False, "approvals": {"count": 1, "required": 1}},  # IN_REVIEW
        "url2": {"draft": True},  # STARTED (lower)
    }
    # Should pick the highest: IN_REVIEW
    assert _infer_state_from_mrs(mrs) == Ticket.State.IN_REVIEW


# --- _extract_variant (line 424) ---


@override_settings(TEATREE_KNOWN_VARIANTS=["Acme", "BigCorp"])
def test_extract_variant_matches_known_variant() -> None:
    """_extract_variant returns the matching known variant (line 424)."""
    result = _extract_variant(["Bug", "acme", "Priority::High"])
    assert result == "Acme"


@override_settings(TEATREE_KNOWN_VARIANTS=["Acme"])
def test_extract_variant_returns_empty_for_unknown() -> None:
    """_extract_variant returns '' when no label matches."""
    result = _extract_variant(["Bug", "Priority::High"])
    assert result == ""


# --- _fetch_review_permalinks (lines 354-410) ---


@override_settings(
    TEATREE_SLACK_TOKEN="",
    TEATREE_REVIEW_CHANNEL_ID="",
)
@pytest.mark.django_db
def test_fetch_review_permalinks_returns_early_without_token() -> None:
    """_fetch_review_permalinks returns early when no token (line 359)."""
    result = SyncResult()
    _fetch_review_permalinks(result)
    assert result.reviews_synced == 0
    assert result.errors == []


@override_settings(
    TEATREE_SLACK_TOKEN="xoxb-token",
    TEATREE_REVIEW_CHANNEL="review-crew",
    TEATREE_REVIEW_CHANNEL_ID="C123",
)
@pytest.mark.django_db
def test_fetch_review_permalinks_skips_draft_mrs() -> None:
    """_fetch_review_permalinks skips draft MRs (line 374)."""
    Ticket.objects.create(
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

    result = SyncResult()
    _fetch_review_permalinks(result)
    # No non-draft MRs → no Slack call
    assert result.reviews_synced == 0


@override_settings(
    TEATREE_SLACK_TOKEN="xoxb-token",
    TEATREE_REVIEW_CHANNEL="review-crew",
    TEATREE_REVIEW_CHANNEL_ID="C123",
)
@pytest.mark.django_db
def test_fetch_review_permalinks_skips_already_linked_mrs() -> None:
    """_fetch_review_permalinks skips MRs that already have review_permalink (line 376-377)."""
    Ticket.objects.create(
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

    result = SyncResult()
    _fetch_review_permalinks(result)
    assert result.reviews_synced == 0


@override_settings(
    TEATREE_SLACK_TOKEN="xoxb-token",
    TEATREE_REVIEW_CHANNEL="review-crew",
    TEATREE_REVIEW_CHANNEL_ID="C123",
)
@pytest.mark.django_db
def test_fetch_review_permalinks_returns_early_when_no_urls() -> None:
    """_fetch_review_permalinks returns early when no eligible MR URLs (line 382-383)."""
    result = SyncResult()
    _fetch_review_permalinks(result)
    assert result.reviews_synced == 0


@override_settings(
    TEATREE_SLACK_TOKEN="xoxb-token",
    TEATREE_REVIEW_CHANNEL="review-crew",
    TEATREE_REVIEW_CHANNEL_ID="C123",
)
@pytest.mark.django_db
def test_fetch_review_permalinks_handles_search_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """_fetch_review_permalinks appends error on exception (line 392-393)."""
    Ticket.objects.create(
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

    monkeypatch.setattr("teetree.backends.slack.search_review_permalinks", _explode)

    result = SyncResult()
    _fetch_review_permalinks(result)
    assert any("Slack review sync" in e for e in result.errors)


@override_settings(
    TEATREE_SLACK_TOKEN="xoxb-token",
    TEATREE_REVIEW_CHANNEL="review-crew",
    TEATREE_REVIEW_CHANNEL_ID="C123",
)
@pytest.mark.django_db
def test_fetch_review_permalinks_stores_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    """_fetch_review_permalinks updates ticket extra with permalink (lines 396-410)."""
    from teetree.backends.slack import SlackReviewMatch  # noqa: PLC0415

    mr_url = "https://gitlab.com/org/repo/-/merge_requests/53"
    Ticket.objects.create(
        issue_url="https://gitlab.com/org/repo/-/issues/503",
        repos=["repo"],
        state=Ticket.State.SHIPPED,
        extra={"mrs": {mr_url: {"draft": False}}},
    )

    monkeypatch.setattr(
        "teetree.backends.slack.search_review_permalinks",
        lambda **kw: [
            SlackReviewMatch(
                mr_url=mr_url,
                permalink="https://team.slack.com/archives/C123/p170000",
                channel="review-crew",
            ),
        ],
    )

    result = SyncResult()
    _fetch_review_permalinks(result)

    assert result.reviews_synced == 1
    ticket = Ticket.objects.get(issue_url="https://gitlab.com/org/repo/-/issues/503")
    mr = ticket.extra["mrs"][mr_url]
    assert mr["review_permalink"] == "https://team.slack.com/archives/C123/p170000"
    assert mr["review_channel"] == "review-crew"


# --- _fetch_review_permalinks: non-dict guards (lines 370, 373, 401, 404) ---


@override_settings(
    TEATREE_SLACK_TOKEN="xoxb-token",
    TEATREE_REVIEW_CHANNEL="review-crew",
    TEATREE_REVIEW_CHANNEL_ID="C123",
)
@pytest.mark.django_db
def test_fetch_review_permalinks_skips_non_dict_mrs_in_collection(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tickets with non-dict mrs are skipped during collection (line 370)."""
    Ticket.objects.create(
        issue_url="https://gitlab.com/org/repo/-/issues/801",
        repos=["repo"],
        state=Ticket.State.SHIPPED,
        extra={"mrs": "not-a-dict"},
    )
    monkeypatch.setattr("teetree.backends.slack.search_review_permalinks", lambda **kw: [])

    result = SyncResult()
    _fetch_review_permalinks(result)
    assert result.reviews_synced == 0


@override_settings(
    TEATREE_SLACK_TOKEN="xoxb-token",
    TEATREE_REVIEW_CHANNEL="review-crew",
    TEATREE_REVIEW_CHANNEL_ID="C123",
)
@pytest.mark.django_db
def test_fetch_review_permalinks_skips_non_dict_mr_entry_in_collection(monkeypatch: pytest.MonkeyPatch) -> None:
    """Individual non-dict MR entries are skipped during collection (line 373)."""
    Ticket.objects.create(
        issue_url="https://gitlab.com/org/repo/-/issues/802",
        repos=["repo"],
        state=Ticket.State.SHIPPED,
        extra={"mrs": {"https://gitlab.com/mr/1": "not-a-dict"}},
    )
    monkeypatch.setattr("teetree.backends.slack.search_review_permalinks", lambda **kw: [])

    result = SyncResult()
    _fetch_review_permalinks(result)
    assert result.reviews_synced == 0


# --- variant extraction in _fetch_issue_labels (lines 323-326) ---


@override_settings(
    TEATREE_GITLAB_TOKEN="test-token",
    TEATREE_GITLAB_USERNAME="testuser",
    TEATREE_KNOWN_VARIANTS=["Acme", "BigCorp"],
)
@pytest.mark.django_db
def test_sync_updates_ticket_variant_from_issue_labels(monkeypatch: pytest.MonkeyPatch) -> None:
    """_fetch_issue_labels extracts variant from labels and saves to ticket (lines 323-326)."""
    Ticket.objects.create(
        issue_url="https://gitlab.com/org/repo/-/issues/600",
        repos=["repo"],
        state=Ticket.State.STARTED,
        extra={},
    )

    mock_client = _make_mock_client([])
    mock_client.get_issue.return_value = {"labels": ["acme", "Bug"], "title": "Fix bug"}
    monkeypatch.setattr("teetree.core.sync.GitLabAPI", lambda **_kw: mock_client)

    sync_followup()

    ticket = Ticket.objects.get(issue_url="https://gitlab.com/org/repo/-/issues/600")
    assert ticket.variant == "Acme"


@pytest.mark.django_db
def test_merge_ticket_extras_combines_mrs_and_repos() -> None:
    """_merge_ticket_extras merges MR entries and repos from source into target."""
    target = Ticket.objects.create(
        issue_url="https://gitlab.com/org/repo/-/issues/900",
        repos=["repo-a"],
        extra={"mrs": {"https://mr/1": {"title": "MR 1"}}},
    )
    source = Ticket.objects.create(
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


@pytest.mark.django_db
def test_merge_ticket_extras_handles_non_dict_mrs() -> None:
    """Non-dict mrs in extras are treated as empty — repos still merge."""
    target = Ticket.objects.create(
        issue_url="https://gitlab.com/org/repo/-/issues/960",
        repos=["repo-a"],
        extra={"mrs": "corrupt"},
    )
    source = Ticket.objects.create(
        issue_url="https://gitlab.com/org/repo/-/issues/961",
        repos=["repo-b"],
        extra={"mrs": ["also-corrupt"]},
    )
    _merge_ticket_extras(target, source)
    target.refresh_from_db()
    assert target.repos == ["repo-a", "repo-b"]


@pytest.mark.django_db
def test_merge_ticket_extras_skips_overlapping_mrs_and_repos() -> None:
    """Overlapping MR URLs and repos are not duplicated."""
    target = Ticket.objects.create(
        issue_url="https://gitlab.com/org/repo/-/issues/950",
        repos=["repo-a", "repo-b"],
        extra={"mrs": {"https://mr/1": {"title": "MR 1"}}},
    )
    source = Ticket.objects.create(
        issue_url="https://gitlab.com/org/repo/-/issues/951",
        repos=["repo-b", "repo-c"],
        extra={"mrs": {"https://mr/1": {"title": "MR 1 dup"}, "https://mr/3": {"title": "MR 3"}}},
    )
    _merge_ticket_extras(target, source)
    target.refresh_from_db()

    assert target.extra["mrs"]["https://mr/1"]["title"] == "MR 1"
    assert "https://mr/3" in target.extra["mrs"]
    assert target.repos == ["repo-a", "repo-b", "repo-c"]


@override_settings(
    TEATREE_OVERLAY_CLASS="tests.teetree_core.conftest.CommandOverlay",
    TEATREE_GITLAB_TOKEN="test-token",
    TEATREE_GITLAB_USERNAME="testuser",
)
@pytest.mark.django_db
def test_sync_deduplicates_tickets_on_upsert(monkeypatch: pytest.MonkeyPatch) -> None:
    """When duplicate tickets exist for the same issue_url, sync merges and deletes extras."""
    ticket_a = Ticket.objects.create(
        issue_url="https://gitlab.com/org/repo/-/issues/100",
        repos=["repo"],
        extra={"mrs": {"https://mr/old": {"title": "old"}}},
        state=Ticket.State.STARTED,
    )
    dup_b = Ticket.objects.create(
        issue_url="https://gitlab.com/org/repo/-/issues/101",
        repos=["other-repo"],
        extra={"mrs": {"https://mr/dup": {"title": "dup"}}},
    )
    dup_c = Ticket.objects.create(
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

    monkeypatch.setattr(Ticket.objects, "filter", patched_filter)

    mock_client = _make_mock_client([_MR_WITH_ISSUE])
    monkeypatch.setattr("teetree.core.sync.GitLabAPI", lambda **_kw: mock_client)

    result = sync_followup()

    assert result.tickets_updated >= 1
    assert not Ticket.objects.filter(pk=dup_b.pk).exists()
    assert not Ticket.objects.filter(pk=dup_c.pk).exists()
    ticket_a.refresh_from_db()
    assert "https://mr/dup" in ticket_a.extra["mrs"]
    assert "other-repo" in ticket_a.repos
