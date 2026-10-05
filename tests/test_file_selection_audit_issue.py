# test-path: cross-cutting
"""The CI selection-audit filer files ONE tracking ticket, not one per PR (#162 Rule 1).

The bare ``gh issue create`` this replaces had no dedupe, so a measured false
negative recurring across PRs filed an identical ticket each time. The load-bearing
assertions here are the two that a regression would break: the filed body carries
the marker a later run keys on, and a run that finds that marker creates NOTHING.
"""

from typing import Any
from unittest.mock import patch

from django.test import TestCase

from scripts.ci.file_selection_audit_issue import _MARKER, build_body, file_audit_issue, main

_REPO = "souliane/teatree"


class _FakeHost:
    """A GitHub host that records creates and serves a canned open backlog."""

    def __init__(self, issues: list[dict[str, Any]] | None = None) -> None:
        self.issues = issues or []
        self.creates: list[dict[str, Any]] = []
        self.updates: list[tuple[str, str]] = []

    def list_repo_open_issues(self, *, repo: str) -> list[dict[str, Any]]:
        return [dict(issue) for issue in self.issues]

    def get_issue(self, issue_url: str) -> dict[str, Any]:
        for issue in self.issues:
            if issue.get("html_url") == issue_url:
                return dict(issue)
        return {"error": f"not found: {issue_url}"}

    def current_user(self) -> str:
        # An installation token cannot read GET /user, so the Rule 5 self-set narrows to empty.
        msg = "Resource not accessible by integration"
        raise RuntimeError(msg)

    def update_issue(self, *, issue_url: str, body: str) -> dict[str, Any]:
        self.updates.append((issue_url, body))
        return {"html_url": issue_url}

    def create_issue(self, *, repo: str, title: str, body: str, labels: list[str] | None = None) -> dict[str, Any]:
        self.creates.append({"repo": repo, "title": title, "body": body, "labels": labels})
        return {"html_url": f"https://github.com/{repo}/issues/{len(self.creates)}"}

    def repo_for_issue_url(self, issue_url: str) -> str:
        return _REPO


def _existing(body: str) -> dict[str, Any]:
    return {
        "html_url": f"https://github.com/{_REPO}/issues/42",
        "title": "push-gate selection-audit: measured false negative (#122)",
        "body": body,
        "state": "open",
        "user": {"login": "github-actions[bot]"},
    }


def _run(host: _FakeHost) -> str:
    with (
        patch("teatree.backends.github.client.GitHubCodeHost", return_value=host),
        patch("teatree.core.issue_hygiene.route_forge_write", side_effect=lambda **kw: kw["text"]),
    ):
        return file_audit_issue(repo=_REPO, token="t", pr_number="7", pr_url=f"https://github.com/{_REPO}/pull/7")


class TestSelectionAuditFiling(TestCase):
    def test_an_empty_backlog_files_one_ticket_carrying_the_dedupe_marker(self) -> None:
        host = _FakeHost()
        line = _run(host)

        assert len(host.creates) == 1
        assert _MARKER in host.creates[0]["body"]
        assert host.creates[0]["labels"] == ["needs-triage"]
        assert "created_new" in line

    def test_a_recurrence_creates_nothing_when_the_marker_is_already_on_the_backlog(self) -> None:
        host = _FakeHost([_existing(build_body(pr_number="6", pr_url="u"))])

        line = _run(host)

        assert host.creates == []
        assert "https://github.com/souliane/teatree/issues/42" in line

    def test_an_unmarked_open_ticket_is_not_mistaken_for_the_tracking_one(self) -> None:
        host = _FakeHost([_existing("Some unrelated ticket about flaky shards.")])

        _run(host)

        assert len(host.creates) == 1


class TestNeverFatal(TestCase):
    def test_a_missing_token_files_nothing_and_still_exits_zero(self) -> None:
        with patch.dict("os.environ", {"GITHUB_REPOSITORY": _REPO, "GH_TOKEN": ""}, clear=False):
            assert main() == 0

    def test_a_forge_failure_exits_zero_rather_than_masking_the_audit_failure(self) -> None:
        env = {"GITHUB_REPOSITORY": _REPO, "GH_TOKEN": "t", "PR_NUMBER": "7", "PR_URL": "u"}
        with (
            patch.dict("os.environ", env, clear=False),
            patch(
                "scripts.ci.file_selection_audit_issue.file_audit_issue",
                side_effect=RuntimeError("forge down"),
            ),
        ):
            assert main() == 0
