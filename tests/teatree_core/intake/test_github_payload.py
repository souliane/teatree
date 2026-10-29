from pathlib import Path

import pytest

from teatree.core.intake.github_payload import HOOK_EVENTS, GitHubPayloadFields
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
            GitHubPayloadFields(actor="", channel_ref="", thread_ref="", body="created", action="created"),
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


@pytest.mark.parametrize(
    ("event", "object_name", "field"),
    [
        ("pull_request_review", "review", "submitted_at"),
        ("pull_request_review_comment", "comment", "updated_at"),
        ("issue_comment", "comment", "updated_at"),
        ("pull_request", "pull_request", "updated_at"),
        ("issues", "issue", "updated_at"),
        ("check_suite", "check_suite", "updated_at"),
        ("check_run", "check_run", "completed_at"),
        ("workflow_run", "workflow_run", "updated_at"),
        ("push", "head_commit", "timestamp"),
    ],
)
def test_event_timestamp_uses_its_own_source(event: str, object_name: str, field: str) -> None:
    payload: RawAPIDict = {object_name: {field: "2026-10-20T09:00:00Z"}, "repository": {"id": 101}}

    fields = GitHubPayloadFields.from_payload(event, payload)

    assert fields.occurred_at is not None
    assert fields.occurred_at.isoformat() == "2026-10-20T09:00:00+00:00"
    assert fields.repo_key == "github:101"


def test_invalid_or_naive_timestamp_is_unknown() -> None:
    for stamp in ("not-a-time", "2026-13-40T09:00:00Z", "2026-10-20T09:00:00"):
        fields = GitHubPayloadFields.from_payload("push", {"head_commit": {"timestamp": stamp}})
        assert fields.occurred_at is None


@pytest.mark.parametrize(
    ("payload", "action", "repo_key"),
    [
        ({"action": "b" * 65}, "b" * 64, ""),
        ({"head_commit": {"timestamp": "0001-01-01T00:00:00+14:00"}}, "", ""),
        ({"head_commit": {"timestamp": "1800-01-01T00:00:00Z"}}, "", ""),
        ({"head_commit": {"timestamp": "2200-01-01T00:00:00Z"}}, "", ""),
        ({"repository": {"id": 10**80}}, "", ""),
        ({"repository": {"id": 0}}, "", ""),
        ({"repository": {"id": True}}, "", ""),
    ],
)
def test_identity_fields_respect_source_bounds(payload: RawAPIDict, action: str, repo_key: str) -> None:
    fields = GitHubPayloadFields.from_payload("push", payload)

    assert fields.action == action
    assert fields.repo_key == repo_key
    assert fields.occurred_at is None


def test_github_subscription_inventory_matches_provisioned_events() -> None:
    root = next((parent for parent in Path(__file__).resolve().parents if (parent / "BLUEPRINT.md").is_file()), None)
    assert root is not None
    document = (root / "docs/blueprint/loop-topology.md").read_text()
    marker = "**GitHub webhook event inventory.**"
    assert marker in document
    section = document.partition(marker)[2].partition("**Forge relay identity.**")[0]
    header = "| Event | Subscription | Current polling reads |"
    assert header in section
    table = section.partition(header)[2].partition("\n\n")[0]
    rows = [
        tuple(cell.strip() for cell in line.strip("|").split("|"))
        for line in table.splitlines()
        if line.startswith("| `")
    ]
    events = {row[0].strip("`"): row[1:] for row in rows}

    assert set(events) == set(HOOK_EVENTS) | {"check_run"}
    assert {event for event, (status, _) in events.items() if status == "Subscribed"} == set(HOOK_EVENTS)
    assert events["check_run"][0] == "Legacy only"
    assert {"ReviewNagScanner", "ReviewRequestResumeScanner", "review_taken_by_other"} <= set(
        events["issue_comment"][1].replace("`", "").replace(",", "").split()
    )
    assert {"GhMainCiStatus", "GhPrApiClient.main_check_failed", "red_set_surface"} <= set(
        events["check_run"][1].replace("`", "").replace(",", "").split()
    )
    assert (
        "| `review` / `ReviewerPrsScanner`, `ReviewedPrHeadScanner` | 300 s | "
        "Review requests, review state, live head, and PR conversation comments. |"
    ) in section
    assert (
        "| `ship` / `MyPrsScanner`, conflict/triage, `PrSweepScanner` | 300 s | "
        "Authored PRs, CI, mergeability, merge state, and default-branch check-runs. |"
    ) in section
    assert (
        "| `followup` / review nag and review request | 1,800 s | "
        "Open PRs, review-request state, and PR conversation comments. |"
    ) in section
    assert (
        "| `housekeeping` / board reconciliation and `SelfUpdateScanner` | 3,600 s | "
        "Forge truth for repair and default-branch check-runs. |"
    ) in section
