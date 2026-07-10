"""Behaviour tests for the wave-1 forge read MCP tools (#3076 / #35).

Each new tool rides an existing :class:`~teatree.core.backend_protocols.CodeHostBackend`
method (no protocol change), resolved through the same ``_forge_client`` seam the
shipped forge reads use. A scripted fake backend keeps the tools hermetic — no
``gh`` / ``glab`` binary, no network — while proving each tool forwards the right
arguments and returns the backend payload verbatim.
"""

from typing import Any
from unittest.mock import patch

from asgiref.sync import async_to_sync
from django.test import TestCase

from teatree.backends.types import Service
from teatree.core.backend_protocols import ApprovalState, PrMergeState, PrOpenState
from teatree.core.overlay import OverlayConfig
from teatree.mcp import build_server


class _GithubOverlay:
    def __init__(self) -> None:
        self.config = OverlayConfig(required_third_party_services=frozenset({Service.GITHUB}))


class _FakeForge:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def search_open_issues(self, *, repo: str, query: str) -> list[dict[str, Any]]:
        self.calls.append(("search_open_issues", {"repo": repo, "query": query}))
        return [{"number": 7, "title": "flaky test"}]

    def list_assigned_issues(self, *, assignee: str) -> list[dict[str, Any]]:
        self.calls.append(("list_assigned_issues", {"assignee": assignee}))
        return [{"number": 9, "assignee": assignee}]

    def list_my_merged_prs(self, *, author: str, updated_after: str | None = None) -> list[dict[str, Any]]:
        self.calls.append(("list_my_merged_prs", {"author": author, "updated_after": updated_after}))
        return [{"number": 3, "author": author}]

    def get_pr_open_state(self, *, pr_url: str) -> PrOpenState:
        self.calls.append(("get_pr_open_state", {"pr_url": pr_url}))
        return PrOpenState.OPEN

    def fetch_pr_merge_state(self, *, slug: str, pr_id: int) -> PrMergeState:
        self.calls.append(("fetch_pr_merge_state", {"slug": slug, "pr_id": pr_id}))
        return PrMergeState(state="OPEN", merge_commit_oid="")

    def fetch_pr_is_draft(self, *, slug: str, pr_id: int) -> bool:
        self.calls.append(("fetch_pr_is_draft", {"slug": slug, "pr_id": pr_id}))
        return False

    def get_pr_author(self, *, pr_url: str) -> str:
        self.calls.append(("get_pr_author", {"pr_url": pr_url}))
        return "octocat"

    def get_mr_approvals(self, *, repo: str, pr_iid: int) -> ApprovalState:
        self.calls.append(("get_mr_approvals", {"repo": repo, "pr_iid": pr_iid}))
        return ApprovalState(approvals_left=1, approved_by=["reviewer"], unresolved_resolvable=2)


def _call(tool: str, args: dict[str, Any], fake: _FakeForge) -> Any:
    with (
        patch("teatree.mcp.server.get_all_overlays", return_value={"a": _GithubOverlay()}),
        patch("teatree.mcp.services_forge._forge_client", return_value=fake),
    ):
        result = async_to_sync(build_server().call_tool)(tool, args)
    structured = result[1] if isinstance(result, tuple) else result
    return structured["result"] if isinstance(structured, dict) and set(structured) == {"result"} else structured


class TestForgeReadTools(TestCase):
    def test_issue_search_forwards_repo_and_query(self) -> None:
        fake = _FakeForge()
        result = _call("github_issue_search", {"repo": "acme/widgets", "query": "flaky"}, fake)

        assert result == [{"number": 7, "title": "flaky test"}]
        assert fake.calls[0] == ("search_open_issues", {"repo": "acme/widgets", "query": "flaky"})

    def test_issue_list_assigned_forwards_assignee(self) -> None:
        fake = _FakeForge()
        result = _call("github_issue_list_assigned", {"assignee": "octocat"}, fake)

        assert result == [{"number": 9, "assignee": "octocat"}]
        assert fake.calls[0] == ("list_assigned_issues", {"assignee": "octocat"})

    def test_my_merged_prs_forwards_author_and_window(self) -> None:
        fake = _FakeForge()
        result = _call(
            "github_my_merged_prs",
            {"author": "octocat", "updated_after": "2026-01-01"},
            fake,
        )

        assert result == [{"number": 3, "author": "octocat"}]
        assert fake.calls[0] == ("list_my_merged_prs", {"author": "octocat", "updated_after": "2026-01-01"})

    def test_pr_get_composes_the_five_backend_reads(self) -> None:
        fake = _FakeForge()
        result = _call(
            "github_pr_get",
            {"repo": "acme/widgets", "pr_iid": 42, "pr_url": "https://github.com/acme/widgets/pull/42"},
            fake,
        )

        assert result["open_state"] == "open"
        assert result["state"] == "OPEN"
        assert result["merged"] is False
        assert result["draft"] is False
        assert result["author"] == "octocat"
        assert result["approvals_left"] == 1
        assert result["approved_by"] == ["reviewer"]
        assert result["unresolved_resolvable"] == 2
