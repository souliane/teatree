import pytest

from teatree.core.intake.github_payload import GitHubPayloadFields
from teatree.types import RawAPIDict


@pytest.mark.parametrize(
    ("event", "payload", "expected"),
    [
        (
            "pull_request_review",
            {"review": {"user": {"login": "rev"}}, "pull_request": {"number": 7, "title": "Tidy"}},
            GitHubPayloadFields(actor="rev", channel_ref="", thread_ref="7", body="Tidy"),
        ),
        (
            "issue_comment",
            {"sender": {"login": "ann"}, "issue": {"number": 3}, "comment": {"body": "Looks good"}},
            GitHubPayloadFields(actor="ann", channel_ref="", thread_ref="3", body="Looks good"),
        ),
        (
            "issues",
            {"repository": {"full_name": "o/r"}, "issue": {"number": 5, "title": "Crash"}},
            GitHubPayloadFields(actor="", channel_ref="o/r", thread_ref="5", body="Crash"),
        ),
        (
            "push",
            {"pusher": {"name": "pat"}, "ref": "refs/heads/main", "head_commit": {"message": "fix"}},
            GitHubPayloadFields(actor="pat", channel_ref="", thread_ref="refs/heads/main", body="fix"),
        ),
        (
            "check_run",
            {"check_run": {"id": 11, "name": "lint", "status": "completed", "conclusion": "failure"}},
            GitHubPayloadFields(actor="", channel_ref="", thread_ref="11", body="lint: failure"),
        ),
        (
            "check_suite",
            {"check_suite": {"id": 12, "status": "queued"}},
            GitHubPayloadFields(actor="", channel_ref="", thread_ref="12", body="check_suite: queued"),
        ),
        (
            "ping",
            {"action": "created", "pull_request": {"number": 1}},
            GitHubPayloadFields(actor="", channel_ref="", thread_ref="", body="created"),
        ),
    ],
)
def test_fields_follow_the_event_family(event: str, payload: RawAPIDict, expected: GitHubPayloadFields) -> None:
    assert GitHubPayloadFields.from_payload(event, payload) == expected


@pytest.mark.parametrize(
    "payload",
    [
        {"sender": "x", "repository": [], "pull_request": {"number": True, "title": 3}},
        {"review": {"user": None}, "pull_request": None},
        {"pusher": "x", "pull_request": {"number": "7"}},
    ],
)
def test_odd_shapes_yield_empty_fields(payload: RawAPIDict) -> None:
    assert GitHubPayloadFields.from_payload("pull_request_review", payload) == GitHubPayloadFields("", "", "", "")
