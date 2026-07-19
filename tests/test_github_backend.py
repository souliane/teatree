"""Tests for teatree.backends.github — GitHub API helpers and GitHubCodeHost."""

import json
import logging
import subprocess
from contextlib import AbstractContextManager
from unittest.mock import MagicMock, patch

import pytest

import teatree.backends.github.api as github_api_mod
import teatree.backends.github.client as github_mod
import teatree.backends.github.projects as github_projects_mod
import teatree.utils.run as utils_run_mod
from teatree.backends.github import GitHubCodeHost, ProjectItem, fetch_project_items, issue_repo_short
from teatree.backends.github.api import (
    _FORGE_READ_TIMEOUT_SECONDS,
    _gh_api_get,
    _gh_api_get_paginated,
    _gh_api_patch,
    _gh_api_post,
    _gh_api_search_paginated,
    _run_gh,
    gh_ambient_auth_available,
)
from teatree.backends.github.projects import _gh_graphql
from teatree.core.backend_protocols import PullRequestSpec


def _reviewthreads_stdout(*, unresolved: int = 0, resolved: int = 0) -> str:
    nodes = [{"isResolved": False}] * unresolved + [{"isResolved": True}] * resolved
    return json.dumps({"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": nodes}}}}})


class TestGetMrApprovals:
    """GitHub aggregate ``reviewDecision`` mapped to an ``ApprovalState`` (#8, F8.3)."""

    def _run_gh_routing(
        self, *, decision_stdout: str, threads_stdout: str
    ) -> AbstractContextManager[MagicMock]:
        """Route the two ``gh`` calls get_mr_approvals now makes.

        ``get_mr_approvals`` reads ``reviewDecision`` (``gh pr view``) AND the
        unresolved review-thread count (``gh api graphql``); a single fixed
        return value can't serve both, so route by whether ``graphql`` is in argv.
        """

        def _route(*args: str, **_: object) -> subprocess.CompletedProcess[str]:
            stdout = threads_stdout if "graphql" in args else decision_stdout
            return subprocess.CompletedProcess([], 0, stdout, "")

        return patch.object(github_mod, "_run_gh", side_effect=_route)

    def _run_gh_returning(self, stdout: str) -> AbstractContextManager[MagicMock]:
        return self._run_gh_routing(decision_stdout=stdout, threads_stdout=_reviewthreads_stdout())

    def test_approved_decision_is_zero_approvals_left(self) -> None:
        with self._run_gh_returning(json.dumps({"reviewDecision": "APPROVED"})) as mock_run:
            state = GitHubCodeHost(token="tok").get_mr_approvals(repo="o/r", pr_iid=9)
        assert state["approvals_left"] == 0
        # The FIRST call is the reviewDecision read (the graphql thread read follows).
        first_args = mock_run.call_args_list[0].args
        assert first_args[:4] == ("gh", "pr", "view", "9")
        assert "reviewDecision" in first_args

    def test_non_approved_decision_is_positive_approvals_left(self) -> None:
        with self._run_gh_returning(json.dumps({"reviewDecision": "REVIEW_REQUIRED"})):
            state = GitHubCodeHost().get_mr_approvals(repo="o/r", pr_iid=9)
        assert state["approvals_left"] == 1

    def test_null_decision_is_not_approved(self) -> None:
        # A PR requiring no review returns ``reviewDecision: null`` — never
        # mis-read as merge-authorised.
        with self._run_gh_returning(json.dumps({"reviewDecision": None})):
            state = GitHubCodeHost().get_mr_approvals(repo="o/r", pr_iid=9)
        assert state["approvals_left"] == 1

    def test_unresolved_review_threads_are_surfaced(self) -> None:
        # F8.3: GitHub DOES gate merge on conversation resolution — the count of
        # open review threads must reach the M7 waiting lane, not be hard-coded 0.
        with self._run_gh_routing(
            decision_stdout=json.dumps({"reviewDecision": "APPROVED"}),
            threads_stdout=_reviewthreads_stdout(unresolved=2, resolved=1),
        ):
            state = GitHubCodeHost().get_mr_approvals(repo="o/r", pr_iid=9)
        assert state["approvals_left"] == 0
        assert state["unresolved_resolvable"] == 2

    def test_all_threads_resolved_reports_zero_unresolved(self) -> None:
        with self._run_gh_routing(
            decision_stdout=json.dumps({"reviewDecision": "APPROVED"}),
            threads_stdout=_reviewthreads_stdout(resolved=3),
        ):
            state = GitHubCodeHost().get_mr_approvals(repo="o/r", pr_iid=9)
        assert state["unresolved_resolvable"] == 0

    def test_unreadable_review_threads_fail_closed(self) -> None:
        # F8.3: an indeterminate thread read must not authorise a merge — fail
        # closed to one unresolved thread rather than a fabricated zero.
        with self._run_gh_routing(
            decision_stdout=json.dumps({"reviewDecision": "APPROVED"}),
            threads_stdout="not json",
        ):
            state = GitHubCodeHost().get_mr_approvals(repo="o/r", pr_iid=9)
        assert state["unresolved_resolvable"] == 1

    def test_thread_read_subprocess_failure_fails_closed(self) -> None:
        def _route(*args: str, **_: object) -> subprocess.CompletedProcess[str]:
            if "graphql" in args:
                raise utils_run_mod.CommandFailedError(["gh"], 1, "", "HTTP 502")
            return subprocess.CompletedProcess([], 0, json.dumps({"reviewDecision": "APPROVED"}), "")

        with patch.object(github_mod, "_run_gh", side_effect=_route):
            state = GitHubCodeHost().get_mr_approvals(repo="o/r", pr_iid=9)
        assert state["unresolved_resolvable"] == 1

    def test_unparsable_decision_is_not_approved(self) -> None:
        with self._run_gh_routing(decision_stdout="not json", threads_stdout=_reviewthreads_stdout()):
            state = GitHubCodeHost().get_mr_approvals(repo="o/r", pr_iid=9)
        assert state["approvals_left"] == 1
        assert state["unresolved_resolvable"] == 0


class TestIssueRepoShort:
    def test_parses_issue_url(self) -> None:
        assert issue_repo_short("https://github.com/souliane/teatree/issues/50") == "teatree"

    def test_parses_pr_url(self) -> None:
        assert issue_repo_short("https://github.com/org/widget/pull/7") == "widget"

    def test_returns_empty_for_unparseable(self) -> None:
        assert issue_repo_short("https://example.com/not/an/issue") == ""

    def test_returns_empty_for_blank(self) -> None:
        assert issue_repo_short("") == ""


class TestRunGh:
    def test_runs_command(self) -> None:
        with patch.object(utils_run_mod.subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 0, "ok", "")
            result = _run_gh("gh", "version")
        mock_run.assert_called_once()
        assert mock_run.call_args.args[0] == ["gh", "version"]
        assert result.stdout == "ok"

    def test_passes_token_via_gh_token_env(self) -> None:
        # Regression for #500: only `gh api` accepts `--header`; injecting it
        # into `gh pr create` fails with `unknown flag --header`.
        with patch.object(utils_run_mod.subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 0, "", "")
            _run_gh("gh", "pr", "create", token="mytoken")
        args = mock_run.call_args[0][0]
        assert "--header" not in args
        assert all("Authorization" not in a for a in args)
        env = mock_run.call_args.kwargs.get("env") or {}
        assert env.get("GH_TOKEN") == "mytoken"

    def test_no_token_does_not_set_gh_token_env(self) -> None:
        with patch.object(utils_run_mod.subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 0, "", "")
            _run_gh("gh", "version")
        env = mock_run.call_args.kwargs.get("env")
        assert env is None or "GH_TOKEN" not in env


class TestGhAmbientAuthAvailable:
    def test_true_when_gh_auth_status_succeeds(self) -> None:
        with patch.object(utils_run_mod.subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(["gh", "auth", "status"], 0, "", "")
            assert gh_ambient_auth_available() is True
        assert mock_run.call_args.args[0] == ["gh", "auth", "status"]

    def test_false_when_gh_auth_status_fails(self) -> None:
        with patch.object(utils_run_mod.subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(["gh", "auth", "status"], 1, "", "not logged in")
            assert gh_ambient_auth_available() is False

    def test_false_when_gh_not_installed(self) -> None:
        with patch.object(utils_run_mod.subprocess, "run", side_effect=FileNotFoundError):
            assert gh_ambient_auth_available() is False


class TestGhApiGet:
    def test_returns_parsed_json(self) -> None:
        with patch.object(github_api_mod, "_run_gh") as mock_run:
            mock_run.return_value = MagicMock(stdout='{"key": "value"}')
            result = _gh_api_get("/repos/test/issues")
        assert result == {"key": "value"}

    def test_passes_token(self) -> None:
        with patch.object(github_api_mod, "_run_gh") as mock_run:
            mock_run.return_value = MagicMock(stdout="{}")
            _gh_api_get("/test", token="tok")
        assert mock_run.call_args[1]["token"] == "tok"

    def test_bounds_the_read_with_a_timeout(self) -> None:
        with patch.object(github_api_mod, "_run_gh") as mock_run:
            mock_run.return_value = MagicMock(stdout="{}")
            _gh_api_get("/test")
        assert mock_run.call_args.kwargs["timeout"] == _FORGE_READ_TIMEOUT_SECONDS


class TestGhApiGetPaginated:
    def test_flattens_slurped_pages(self) -> None:
        pages = [[{"id": 1}, {"id": 2}], [{"id": 3}]]
        with patch.object(github_api_mod, "_run_gh") as mock_run:
            mock_run.return_value = MagicMock(stdout=json.dumps(pages))
            result = _gh_api_get_paginated("repos/o/r/issues/5/comments?per_page=100")
        argv = mock_run.call_args.args
        assert "--paginate" in argv
        assert "--slurp" in argv
        assert "repos/o/r/issues/5/comments?per_page=100" in argv
        assert result == [{"id": 1}, {"id": 2}, {"id": 3}]

    def test_passes_token(self) -> None:
        with patch.object(github_api_mod, "_run_gh") as mock_run:
            mock_run.return_value = MagicMock(stdout="[]")
            _gh_api_get_paginated("repos/o/r/issues/5/comments", token="tok")
        assert mock_run.call_args.kwargs["token"] == "tok"

    def test_bounds_the_read_with_a_timeout(self) -> None:
        with patch.object(github_api_mod, "_run_gh") as mock_run:
            mock_run.return_value = MagicMock(stdout="[]")
            _gh_api_get_paginated("repos/o/r/issues/5/comments")
        assert mock_run.call_args.kwargs["timeout"] == _FORGE_READ_TIMEOUT_SECONDS

    def test_empty_pages_return_empty_list(self) -> None:
        with patch.object(github_api_mod, "_run_gh") as mock_run:
            mock_run.return_value = MagicMock(stdout="[]")
            result = _gh_api_get_paginated("repos/o/r/issues/5/comments")
        assert result == []

    def test_non_array_outer_payload_returns_empty_list(self) -> None:
        # A single-object endpoint accidentally passed here must not explode;
        # ``--slurp`` always yields an outer array, but guard defensively.
        with patch.object(github_api_mod, "_run_gh") as mock_run:
            mock_run.return_value = MagicMock(stdout='{"message": "Not Found"}')
            result = _gh_api_get_paginated("repos/o/r/issues/5/comments")
        assert result == []

    def test_skips_non_list_pages(self) -> None:
        with patch.object(github_api_mod, "_run_gh") as mock_run:
            mock_run.return_value = MagicMock(stdout=json.dumps([[{"id": 1}], {"oops": True}]))
            result = _gh_api_get_paginated("repos/o/r/issues/5/comments")
        assert result == [{"id": 1}]


class TestGhApiSearchPaginated:
    def test_flattens_items_across_pages(self) -> None:
        pages = [{"items": [{"number": 1}, {"number": 2}]}, {"items": [{"number": 3}]}]
        with patch.object(github_api_mod, "_run_gh") as mock_run:
            mock_run.return_value = MagicMock(stdout=json.dumps(pages))
            result = _gh_api_search_paginated("search/issues?q=is:pr")
        assert result == [{"number": 1}, {"number": 2}, {"number": 3}]

    def test_bounds_the_read_with_a_timeout(self) -> None:
        # A stalled forge must degrade (TimeoutExpired -> caller fail-open), not
        # hang the ship indefinitely — the GitHub search read carries a bound.
        with patch.object(github_api_mod, "_run_gh") as mock_run:
            mock_run.return_value = MagicMock(stdout="[]")
            _gh_api_search_paginated("search/issues?q=is:pr")
        assert mock_run.call_args.kwargs["timeout"] == _FORGE_READ_TIMEOUT_SECONDS

    def test_non_list_payload_returns_empty(self) -> None:
        with patch.object(github_api_mod, "_run_gh") as mock_run:
            mock_run.return_value = MagicMock(stdout='{"message": "Not Found"}')
            assert _gh_api_search_paginated("search/issues?q=is:pr") == []

    def test_skips_non_dict_pages_and_missing_items(self) -> None:
        pages = ["oops", {"total_count": 0}, {"items": [{"number": 5}]}]
        with patch.object(github_api_mod, "_run_gh") as mock_run:
            mock_run.return_value = MagicMock(stdout=json.dumps(pages))
            assert _gh_api_search_paginated("search/issues?q=is:pr") == [{"number": 5}]


class TestRunGhTimeout:
    def test_threads_timeout_into_subprocess(self) -> None:
        with patch.object(utils_run_mod.subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 0, "", "")
            _run_gh("gh", "api", "x", timeout=12.5)
        assert mock_run.call_args.kwargs["timeout"] == pytest.approx(12.5)

    def test_default_leaves_the_subprocess_unbounded(self) -> None:
        with patch.object(utils_run_mod.subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 0, "", "")
            _run_gh("gh", "version")
        assert mock_run.call_args.kwargs["timeout"] is None


class TestGhApiPost:
    def test_sends_payload_via_stdin(self) -> None:
        with patch.object(utils_run_mod.subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 0, '{"id": 1}', "")
            result = _gh_api_post("/test", {"body": "hello"})
        assert result == {"id": 1}
        call_kwargs = mock_run.call_args[1]
        assert json.loads(call_kwargs["input"]) == {"body": "hello"}

    def test_passes_token_via_env_not_argv(self) -> None:
        with patch.object(utils_run_mod.subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 0, "{}", "")
            _gh_api_post("/test", {}, token="tok")
        args = mock_run.call_args[0][0]
        assert not any("Authorization" in arg for arg in args)
        assert mock_run.call_args.kwargs["env"]["GH_TOKEN"] == "tok"

    def test_bounds_timeout(self) -> None:
        with patch.object(utils_run_mod.subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 0, "{}", "")
            _gh_api_post("/test", {}, token="tok")
        assert mock_run.call_args.kwargs["timeout"] == _FORGE_READ_TIMEOUT_SECONDS


class TestGhApiPatch:
    def test_sends_patch_request(self) -> None:
        with patch.object(utils_run_mod.subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 0, '{"updated": true}', "")
            result = _gh_api_patch("/test/1", {"title": "new"})
        assert result == {"updated": True}
        args = mock_run.call_args[0][0]
        assert "--method" in args
        assert "PATCH" in args

    def test_passes_token_via_env_not_argv(self) -> None:
        with patch.object(utils_run_mod.subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 0, "{}", "")
            _gh_api_patch("/test", {}, token="tok")
        args = mock_run.call_args[0][0]
        assert not any("Authorization" in arg for arg in args)
        assert mock_run.call_args.kwargs["env"]["GH_TOKEN"] == "tok"


class TestGhGraphql:
    def test_executes_query(self) -> None:
        with patch.object(utils_run_mod.subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 0, '{"data": {}}', "")
            result = _gh_graphql("{ viewer { login } }")
        assert result == {"data": {}}
        args = mock_run.call_args[0][0]
        assert "graphql" in args

    def test_passes_token_via_env_not_argv(self) -> None:
        with patch.object(utils_run_mod.subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 0, "{}", "")
            _gh_graphql("{ test }", token="tok")
        args = mock_run.call_args[0][0]
        assert not any("Authorization" in arg for arg in args)
        assert mock_run.call_args.kwargs["env"]["GH_TOKEN"] == "tok"


class TestFetchProjectItems:
    def test_parses_project_items(self) -> None:
        graphql_response = {
            "data": {
                "user": {
                    "projectV2": {
                        "items": {
                            "nodes": [
                                {
                                    "fieldValueByName": {"name": "Todo"},
                                    "content": {
                                        "number": 42,
                                        "title": "Fix bug",
                                        "url": "https://github.com/org/repo/issues/42",
                                        "updatedAt": "2026-04-01T00:00:00Z",
                                        "labels": {"nodes": [{"name": "bug"}]},
                                    },
                                },
                            ]
                        }
                    }
                }
            }
        }
        with patch.object(github_projects_mod, "_gh_graphql", return_value=graphql_response):
            items = fetch_project_items("testuser", 1)
        assert len(items) == 1
        assert items[0] == ProjectItem(
            issue_number=42,
            title="Fix bug",
            url="https://github.com/org/repo/issues/42",
            status="Todo",
            position=0,
            labels=["bug"],
            updated_at="2026-04-01T00:00:00Z",
        )

    def test_returns_empty_for_missing_project(self) -> None:
        with patch.object(github_projects_mod, "_gh_graphql", return_value={"data": {"user": {}}}):
            items = fetch_project_items("testuser", 1)
        assert items == []

    @pytest.mark.parametrize(
        "graphql_response",
        [
            {"data": {"user": None}},
            {"data": {"user": {"projectV2": None}}},
            {"data": {"user": {"projectV2": {"items": None}}}},
            {"data": {"user": {"projectV2": {"items": {"nodes": None}}}}},
            {"data": None},
        ],
    )
    def test_returns_empty_when_graphql_hop_is_null(self, graphql_response: dict[str, object]) -> None:
        with patch.object(github_projects_mod, "_gh_graphql", return_value=graphql_response):
            items = fetch_project_items("testuser", 1)
        assert items == []

    def test_handles_null_labels_block(self) -> None:
        graphql_response = {
            "data": {
                "user": {
                    "projectV2": {
                        "items": {
                            "nodes": [
                                {
                                    "fieldValueByName": {"name": "Todo"},
                                    "content": {
                                        "number": 7,
                                        "title": "No labels block",
                                        "url": "https://github.com/org/repo/issues/7",
                                        "labels": None,
                                    },
                                },
                            ]
                        }
                    }
                }
            }
        }
        with patch.object(github_projects_mod, "_gh_graphql", return_value=graphql_response):
            items = fetch_project_items("testuser", 1)
        assert len(items) == 1
        assert items[0].labels == []

    def test_skips_non_dict_nodes(self) -> None:
        graphql_response = {"data": {"user": {"projectV2": {"items": {"nodes": [None, "invalid"]}}}}}
        with patch.object(github_projects_mod, "_gh_graphql", return_value=graphql_response):
            items = fetch_project_items("testuser", 1)
        assert items == []

    def test_skips_draft_items(self) -> None:
        graphql_response = {
            "data": {
                "user": {
                    "projectV2": {
                        "items": {
                            "nodes": [
                                {
                                    "fieldValueByName": None,
                                    "content": {},  # draft item with no number
                                },
                            ]
                        }
                    }
                }
            }
        }
        with patch.object(github_projects_mod, "_gh_graphql", return_value=graphql_response):
            items = fetch_project_items("testuser", 1)
        assert items == []

    def test_handles_null_status_field(self) -> None:
        graphql_response = {
            "data": {
                "user": {
                    "projectV2": {
                        "items": {
                            "nodes": [
                                {
                                    "fieldValueByName": None,
                                    "content": {
                                        "number": 1,
                                        "title": "No status",
                                        "url": "https://github.com/org/repo/issues/1",
                                        "labels": {"nodes": []},
                                    },
                                },
                            ]
                        }
                    }
                }
            }
        }
        with patch.object(github_projects_mod, "_gh_graphql", return_value=graphql_response):
            items = fetch_project_items("testuser", 1)
        assert len(items) == 1
        assert items[0].status == ""

    def test_follows_pagination_across_pages(self) -> None:
        def _page(number: int, *, has_next: bool, cursor: str) -> dict[str, object]:
            return {
                "data": {
                    "user": {
                        "projectV2": {
                            "items": {
                                "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
                                "nodes": [
                                    {
                                        "fieldValueByName": {"name": "Todo"},
                                        "content": {
                                            "number": number,
                                            "title": f"Issue {number}",
                                            "url": f"https://github.com/org/repo/issues/{number}",
                                            "updatedAt": "2026-04-01T00:00:00Z",
                                            "labels": {"nodes": []},
                                        },
                                    }
                                ],
                            }
                        }
                    }
                }
            }

        pages = [
            _page(1, has_next=True, cursor="CURSOR_PAGE_1"),
            _page(2, has_next=False, cursor="CURSOR_PAGE_2"),
        ]
        with patch.object(github_projects_mod, "_gh_graphql", side_effect=pages) as mock_graphql:
            items = fetch_project_items("testuser", 1)
        assert mock_graphql.call_count == 2
        assert [item.issue_number for item in items] == [1, 2]
        assert [item.position for item in items] == [0, 1]
        assert "CURSOR_PAGE_1" in mock_graphql.call_args_list[1].args[0]


class TestGitHubCodeHost:
    def test_create_pr(self) -> None:
        """#1222: GitHub backend returns the canonical ``web_url`` field.

        Cross-host code paths (notably ``ShipExecutor``) read ``web_url`` —
        GitLab's API native key. Returning ``url`` here silently produced
        empty PR rows on the ticket and tricked downstream gates into
        thinking the PR was missing.
        """
        with patch.object(github_mod, "_run_gh") as mock_run:
            mock_run.return_value = MagicMock(stdout="https://github.com/org/repo/pull/1\n")
            host = GitHubCodeHost(token="tok")
            result = host.create_pr(
                PullRequestSpec(
                    repo="org/repo",
                    branch="feature",
                    title="Add feature",
                    description="Description",
                ),
            )
        assert result == {"web_url": "https://github.com/org/repo/pull/1"}

    def test_create_pr_raises_when_gh_returns_no_url(self) -> None:
        """#1226: an empty/non-URL ``gh pr create`` stdout must surface as a failure.

        ``gh pr create`` can exit 0 while printing a non-URL line (e.g. the
        "no commits between" pre-push race). The backend MUST refuse to claim
        success with an empty URL; the ship runner relies on this invariant
        to flip ``ok=False`` instead of advancing the FSM with an empty
        ``pr_urls`` entry.
        """
        import pytest  # noqa: PLC0415

        from teatree.utils.run import CommandFailedError  # noqa: PLC0415

        with patch.object(github_mod, "_run_gh") as mock_run:
            mock_run.return_value = MagicMock(stdout="\n")
            host = GitHubCodeHost(token="tok")
            with pytest.raises(CommandFailedError):
                host.create_pr(
                    PullRequestSpec(
                        repo="org/repo",
                        branch="feature",
                        title="Add feature",
                        description="Description",
                    ),
                )

    def test_create_pr_resolves_local_path_to_owner_repo_slug(self, tmp_path: object) -> None:
        """``gh pr create --repo`` requires ``owner/repo`` — local paths must be resolved first."""
        with (
            patch.object(github_mod, "_run_gh") as mock_run,
            patch.object(github_mod.git, "remote_slug", return_value="souliane/teatree") as mock_slug,
        ):
            mock_run.return_value = MagicMock(stdout="https://github.com/souliane/teatree/pull/3\n")
            host = GitHubCodeHost()
            host.create_pr(
                PullRequestSpec(
                    repo="/Users/adrien/workspace/ticket/teatree",
                    branch="feature",
                    title="t",
                    description="d",
                ),
            )
        mock_slug.assert_called_once_with(repo="/Users/adrien/workspace/ticket/teatree")
        cmd = list(mock_run.call_args[0])
        assert cmd[cmd.index("--repo") + 1] == "souliane/teatree"

    def test_create_pr_passes_through_existing_slug_unchanged(self) -> None:
        """When the caller already provides ``owner/repo``, no resolution is needed."""
        with patch.object(github_mod, "_run_gh") as mock_run:
            mock_run.return_value = MagicMock(stdout="https://github.com/org/repo/pull/4\n")
            host = GitHubCodeHost()
            host.create_pr(
                PullRequestSpec(
                    repo="org/repo",
                    branch="feature",
                    title="t",
                    description="d",
                ),
            )
        cmd = list(mock_run.call_args[0])
        assert cmd[cmd.index("--repo") + 1] == "org/repo"

    def test_create_pr_with_optional_params(self) -> None:
        with patch.object(github_mod, "_run_gh") as mock_run:
            mock_run.return_value = MagicMock(stdout="https://github.com/org/repo/pull/2\n")
            host = GitHubCodeHost()
            host.create_pr(
                PullRequestSpec(
                    repo="org/repo",
                    branch="feature",
                    title="Title",
                    description="Desc",
                    target_branch="develop",
                    labels=["bug", "urgent"],
                    assignee="user1",
                ),
            )
        args = mock_run.call_args[0]
        cmd = list(args)
        # Flatten for checking
        flat = []
        for a in cmd:
            if isinstance(a, (list, tuple)):
                flat.extend(a)
            else:
                flat.append(a)
        assert "--base" in flat
        assert "develop" in flat
        assert "--label" in flat
        assert "--assignee" in flat

    def test_current_user_returns_login(self) -> None:
        with patch.object(github_mod, "_gh_api_get", return_value={"login": "souliane", "id": 42}) as mock_get:
            host = GitHubCodeHost(token="tok")
            result = host.current_user()
        assert result == "souliane"
        mock_get.assert_called_once_with("user", token="tok")

    def test_current_user_returns_empty_when_api_non_dict(self) -> None:
        with patch.object(github_mod, "_gh_api_get", return_value=["unexpected"]):
            host = GitHubCodeHost()
            result = host.current_user()
        assert result == ""

    def test_current_user_returns_empty_when_login_missing(self) -> None:
        with patch.object(github_mod, "_gh_api_get", return_value={"id": 42}):
            host = GitHubCodeHost()
            result = host.current_user()
        assert result == ""

    def test_create_sub_issue_is_unsupported(self) -> None:
        host = GitHubCodeHost(token="tok")
        result = host.create_sub_issue(
            parent_url="https://github.com/org/repo/issues/8",
            title="child",
            body="b",
            child_type="Task",
        )
        assert "not supported" in result["error"]

    def test_repo_for_issue_url_returns_owner_repo(self) -> None:
        host = GitHubCodeHost(token="tok")
        # The note's own repo — evidence uploads target this, not a 2nd repo.
        assert host.repo_for_issue_url("https://github.com/owner/product/issues/42") == "owner/product"
        # A non-issue URL yields "".
        assert host.repo_for_issue_url("https://github.com/owner/product/pull/7") == ""

    def test_list_my_prs_searches_by_author_across_forge(self) -> None:
        items = [
            {"number": 1, "title": "first", "html_url": "https://github.com/org/repo/pull/1"},
            {"number": 2, "title": "second", "html_url": "https://github.com/org/other/pull/2"},
        ]
        with patch.object(github_mod, "_gh_api_search_paginated", return_value=items) as mock_search:
            host = GitHubCodeHost(token="tok")
            result = host.list_my_prs(author="alice")
        assert len(result) == 2
        assert result[0]["number"] == 1
        mock_search.assert_called_once_with(
            "search/issues?q=is%3Apr+is%3Aopen+author%3Aalice&per_page=100",
            token="tok",
        )

    def test_list_my_prs_returns_empty_when_no_results(self) -> None:
        with patch.object(github_mod, "_gh_api_search_paginated", return_value=[]):
            host = GitHubCodeHost()
            assert host.list_my_prs(author="alice") == []

    def test_list_my_prs_paginates_beyond_first_page(self) -> None:
        # A factory with >100 open PRs hits GitHub search's 100-item cap; items
        # on page 2 are silently dropped, breaking the PR-sweep/followup scanners.
        page_one = [{"number": i} for i in range(100)]
        page_two = [{"number": 100}]
        slurped = json.dumps([{"items": page_one}, {"items": page_two}])
        with patch.object(github_api_mod, "_run_gh") as mock_run:
            mock_run.return_value = MagicMock(stdout=slurped)
            host = GitHubCodeHost()
            result = host.list_my_prs(author="alice")
        assert len(result) == 101
        assert {"number": 100} in result
        argv = mock_run.call_args_list[0].args
        assert "--paginate" in argv

    def test_list_review_requested_prs_searches_by_reviewer(self) -> None:
        items = [{"number": 7, "title": "needs review"}]
        with patch.object(github_mod, "_gh_api_search_paginated", return_value=items) as mock_search:
            host = GitHubCodeHost(token="tok")
            result = host.list_review_requested_prs(reviewer="alice")
        assert len(result) == 1
        assert result[0]["number"] == 7
        mock_search.assert_called_once_with(
            "search/issues?q=is%3Apr+is%3Aopen+review-requested%3Aalice&per_page=100",
            token="tok",
        )

    def test_list_review_requested_prs_returns_empty_when_no_results(self) -> None:
        with patch.object(github_mod, "_gh_api_search_paginated", return_value=[]):
            host = GitHubCodeHost()
            assert host.list_review_requested_prs(reviewer="alice") == []

    def test_list_review_requested_prs_paginates_beyond_first_page(self) -> None:
        page_one = [{"number": i} for i in range(100)]
        page_two = [{"number": 100}]
        slurped = json.dumps([{"items": page_one}, {"items": page_two}])
        with patch.object(github_api_mod, "_run_gh") as mock_run:
            mock_run.return_value = MagicMock(stdout=slurped)
            host = GitHubCodeHost()
            result = host.list_review_requested_prs(reviewer="alice")
        assert len(result) == 101
        assert {"number": 100} in result
        argv = mock_run.call_args_list[0].args
        assert "--paginate" in argv

    def test_list_assigned_issues_searches_by_assignee(self) -> None:
        items = [{"number": 11, "title": "bug to fix"}]
        with patch.object(github_mod, "_gh_api_search_paginated", return_value=items) as mock_search:
            host = GitHubCodeHost(token="tok")
            result = host.list_assigned_issues(assignee="alice")
        assert len(result) == 1
        mock_search.assert_called_once_with(
            "search/issues?q=is%3Aissue+is%3Aopen+assignee%3Aalice&per_page=100",
            token="tok",
        )

    def test_list_assigned_issues_returns_empty_when_no_results(self) -> None:
        with patch.object(github_mod, "_gh_api_search_paginated", return_value=[]):
            host = GitHubCodeHost()
            assert host.list_assigned_issues(assignee="alice") == []

    def test_list_assigned_issues_paginates_beyond_first_page(self) -> None:
        page_one = [{"number": i} for i in range(100)]
        page_two = [{"number": 100}]
        slurped = json.dumps([{"items": page_one}, {"items": page_two}])
        with patch.object(github_api_mod, "_run_gh") as mock_run:
            mock_run.return_value = MagicMock(stdout=slurped)
            host = GitHubCodeHost()
            result = host.list_assigned_issues(assignee="alice")
        assert len(result) == 101
        assert {"number": 100} in result
        argv = mock_run.call_args_list[0].args
        assert "--paginate" in argv

    def test_list_authored_issues_searches_by_author(self) -> None:
        """#3235 — the author-scoped intake query: issues the trusted human FILED."""
        items = [{"number": 11, "title": "feature request"}]
        with patch.object(github_mod, "_gh_api_search_paginated", return_value=items) as mock_search:
            host = GitHubCodeHost(token="tok")
            result = host.list_authored_issues(author="souliane")
        assert len(result) == 1
        mock_search.assert_called_once_with(
            "search/issues?q=is%3Aissue+is%3Aopen+author%3Asouliane&per_page=100",
            token="tok",
        )

    def test_list_authored_issues_scopes_search_to_repo_slugs(self) -> None:
        """repo_slugs AND OR-ed ``repo:owner/name`` qualifiers into the search — the cross-repo firehose fix."""
        with patch.object(github_mod, "_gh_api_search_paginated", return_value=[]) as mock_search:
            host = GitHubCodeHost(token="tok")
            host.list_authored_issues(
                author="souliane",
                repo_slugs=("souliane/teatree", "souliane/other"),
            )
        query = mock_search.call_args.args[0]
        assert "repo%3Asouliane%2Fteatree" in query
        assert "repo%3Asouliane%2Fother" in query
        assert "author%3Asouliane" in query

    def test_list_authored_issues_without_repo_slugs_is_unscoped(self) -> None:
        """Empty repo_slugs keeps today's global author query (back-compat)."""
        with patch.object(github_mod, "_gh_api_search_paginated", return_value=[]) as mock_search:
            host = GitHubCodeHost(token="tok")
            host.list_authored_issues(author="souliane")
        query = mock_search.call_args.args[0]
        assert "repo%3A" not in query

    def test_list_authored_issues_returns_empty_when_no_results(self) -> None:
        with patch.object(github_mod, "_gh_api_search_paginated", return_value=[]):
            host = GitHubCodeHost()
            assert host.list_authored_issues(author="souliane") == []

    def test_list_authored_issues_paginates_beyond_first_page(self) -> None:
        page_one = [{"number": i} for i in range(100)]
        page_two = [{"number": 100}]
        slurped = json.dumps([{"items": page_one}, {"items": page_two}])
        with patch.object(github_api_mod, "_run_gh") as mock_run:
            mock_run.return_value = MagicMock(stdout=slurped)
            host = GitHubCodeHost()
            result = host.list_authored_issues(author="souliane")
        assert len(result) == 101
        assert {"number": 100} in result
        assert "--paginate" in mock_run.call_args_list[0].args

    def test_create_issue_posts_payload_with_labels(self) -> None:
        created = {"html_url": "https://github.com/org/repo/issues/9", "number": 9}
        with patch.object(github_mod, "_gh_api_post", return_value=created) as mock_post:
            host = GitHubCodeHost(token="tok")
            result = host.create_issue(repo="org/repo", title="t", body="b", labels=["enforcement-gap"])
        assert result == created
        mock_post.assert_called_once_with(
            "repos/org/repo/issues",
            {"title": "t", "body": "b", "labels": ["enforcement-gap"]},
            token="tok",
        )

    def test_create_issue_returns_empty_for_non_dict(self) -> None:
        with patch.object(github_mod, "_gh_api_post", return_value="oops"):
            host = GitHubCodeHost()
            assert host.create_issue(repo="org/repo", title="t", body="b") == {}

    def test_search_open_issues_filters_to_repo_and_returns_items(self) -> None:
        with patch.object(github_mod, "_gh_api_search_paginated", return_value=[{"number": 9}]) as mock_search:
            host = GitHubCodeHost(token="tok")
            result = host.search_open_issues(repo="org/repo", query="fingerprint:abc")
        assert result == [{"number": 9}]
        endpoint = mock_search.call_args[0][0]
        assert "repo%3Aorg%2Frepo" in endpoint
        assert "is%3Aopen" in endpoint

    def test_search_open_issues_returns_empty_when_no_results(self) -> None:
        with patch.object(github_mod, "_gh_api_search_paginated", return_value=[]):
            host = GitHubCodeHost()
            assert host.search_open_issues(repo="org/repo", query="x") == []

    def test_search_open_issues_paginates_beyond_first_page(self) -> None:
        page_one = [{"number": i} for i in range(100)]
        page_two = [{"number": 100}]
        slurped = json.dumps([{"items": page_one}, {"items": page_two}])
        with patch.object(github_api_mod, "_run_gh") as mock_run:
            mock_run.return_value = MagicMock(stdout=slurped)
            host = GitHubCodeHost()
            result = host.search_open_issues(repo="org/repo", query="fingerprint:abc")
        assert len(result) == 101
        assert {"number": 100} in result
        argv = mock_run.call_args_list[0].args
        assert "--paginate" in argv

    def test_close_issue_patches_state_closed(self) -> None:
        url = "https://github.com/org/repo/issues/9"
        with patch.object(github_mod, "_gh_api_patch", return_value={"state": "closed"}) as mock_patch:
            host = GitHubCodeHost(token="tok")
            result = host.close_issue(issue_url=url)
        assert result == {"state": "closed"}
        mock_patch.assert_called_once_with(
            "repos/org/repo/issues/9",
            {"state": "closed", "state_reason": "not_planned"},
            token="tok",
        )

    def test_close_issue_posts_audit_comment_first(self) -> None:
        url = "https://github.com/org/repo/issues/9"
        with (
            patch.object(github_mod, "_gh_api_post", return_value={"id": 1}) as mock_post,
            patch.object(github_mod, "_gh_api_patch", return_value={"state": "closed"}),
        ):
            GitHubCodeHost().close_issue(issue_url=url, comment="dead")
        assert mock_post.call_args[0][0] == "repos/org/repo/issues/9/comments"
        assert mock_post.call_args[0][1] == {"body": "dead"}

    def test_close_issue_rejects_non_issue_url(self) -> None:
        with patch.object(github_mod, "_gh_api_patch") as mock_patch:
            result = GitHubCodeHost().close_issue(issue_url="https://example.com/not/an/issue")
        assert "error" in result
        mock_patch.assert_not_called()

    def test_update_issue_patches_the_body(self) -> None:
        url = "https://github.com/org/repo/issues/9"
        with patch.object(github_mod, "_gh_api_patch", return_value={"number": 9}) as mock_patch:
            host = GitHubCodeHost(token="tok")
            result = host.update_issue(issue_url=url, body="new umbrella body")
        assert result == {"number": 9}
        mock_patch.assert_called_once_with(
            "repos/org/repo/issues/9",
            {"body": "new umbrella body"},
            token="tok",
        )

    def test_update_issue_rejects_non_issue_url(self) -> None:
        with patch.object(github_mod, "_gh_api_patch") as mock_patch:
            result = GitHubCodeHost().update_issue(issue_url="https://example.com/not/an/issue", body="x")
        assert "error" in result
        mock_patch.assert_not_called()

    def test_post_pr_comment(self) -> None:
        with patch.object(github_mod, "_gh_api_post", return_value={"id": 42}) as mock_post:
            host = GitHubCodeHost()
            result = host.post_pr_comment(repo="org/repo", pr_iid=5, body="LGTM")
        assert result == {"id": 42}
        mock_post.assert_called_once()

    def test_post_pr_comment_returns_empty_for_non_dict(self) -> None:
        with patch.object(github_mod, "_gh_api_post", return_value="error"):
            host = GitHubCodeHost()
            result = host.post_pr_comment(repo="org/repo", pr_iid=5, body="test")
        assert result == {}

    def test_update_pr_comment(self) -> None:
        with patch.object(github_mod, "_gh_api_patch", return_value={"id": 42}) as mock_patch:
            host = GitHubCodeHost()
            result = host.update_pr_comment(repo="org/repo", pr_iid=5, comment_id=42, body="Updated")
        assert result == {"id": 42}
        # GitHub comment IDs are globally unique — pr_iid is unused on PATCH path
        mock_patch.assert_called_once_with(
            "repos/org/repo/issues/comments/42",
            {"body": "Updated"},
            token="",
        )

    def test_update_pr_comment_returns_empty_for_non_dict(self) -> None:
        with patch.object(github_mod, "_gh_api_patch", return_value=[]):
            host = GitHubCodeHost()
            result = host.update_pr_comment(repo="org/repo", pr_iid=5, comment_id=42, body="x")
        assert result == {}

    def test_list_pr_comments(self) -> None:
        # ``--slurp`` wraps each page in an outer array; one page → one inner list.
        notes = [{"id": 1, "body": "comment"}]
        with patch.object(github_mod, "_gh_api_get_paginated", return_value=notes):
            host = GitHubCodeHost()
            result = host.list_pr_comments(repo="org/repo", pr_iid=5)
        assert result == notes

    def test_list_pr_comments_returns_empty_for_non_list(self) -> None:
        with patch.object(github_mod, "_gh_api_get_paginated", return_value=[]):
            host = GitHubCodeHost()
            result = host.list_pr_comments(repo="org/repo", pr_iid=5)
        assert result == []

    def test_list_pr_comments_paginates_beyond_the_first_page(self) -> None:
        # A PR with >30 comments: GitHub's default page size silently caps a
        # non-paginated GET at 30, so a ``## Test Plan`` note older than the 30
        # most-recent comments goes unseen and the evidence-poster duplicates
        # it every run. ``gh api --paginate --slurp`` returns every page as an
        # outer array of per-page arrays; the helper flattens them so the
        # dedup search sees the full comment history.
        page_one = [{"id": i, "body": f"c{i}"} for i in range(100)]
        page_two = [{"id": 100, "body": "## Test Plan"}]
        slurped_pages = json.dumps([page_one, page_two])
        with patch.object(github_api_mod, "_run_gh") as mock_run:
            mock_run.return_value = MagicMock(stdout=slurped_pages)
            host = GitHubCodeHost()
            result = host.list_pr_comments(repo="org/repo", pr_iid=5)
        argv = mock_run.call_args.args
        assert "--paginate" in argv
        assert "--slurp" in argv
        assert result == [*page_one, *page_two]
        assert {"id": 100, "body": "## Test Plan"} in result

    def test_list_pr_comments_returns_empty_when_slurp_yields_no_pages(self) -> None:
        # No comments → ``--slurp`` emits ``[]`` (zero pages) → empty result.
        with patch.object(github_api_mod, "_run_gh") as mock_run:
            mock_run.return_value = MagicMock(stdout="[]")
            host = GitHubCodeHost()
            result = host.list_pr_comments(repo="org/repo", pr_iid=5)
        assert result == []

    def test_list_issue_comments_returns_payload(self) -> None:
        notes = [{"id": 1, "body": "x"}]
        with patch.object(github_mod, "_gh_api_get_paginated", return_value=notes) as mock_get:
            host = GitHubCodeHost()
            result = host.list_issue_comments(issue_url="https://github.com/souliane/teatree/issues/7")
        assert result == notes
        assert "repos/souliane/teatree/issues/7/comments" in mock_get.call_args.args[0]

    def test_list_issue_comments_returns_empty_for_non_issue_url(self) -> None:
        host = GitHubCodeHost()
        result = host.list_issue_comments(issue_url="https://github.com/souliane/teatree/pull/7")
        assert result == []

    def test_list_issue_comments_paginates_beyond_the_first_page(self) -> None:
        # A busy issue with >100 comments: a non-paginated GET silently caps at
        # GitHub's default page size, so a ``## Test Plan`` note past the first
        # page goes unseen and the evidence-poster duplicates it every run.
        page_one = [{"id": i, "body": f"c{i}"} for i in range(100)]
        page_two = [{"id": 100, "body": "## Test Plan"}]
        slurped_pages = json.dumps([page_one, page_two])
        with patch.object(github_api_mod, "_run_gh") as mock_run:
            mock_run.return_value = MagicMock(stdout=slurped_pages)
            host = GitHubCodeHost()
            result = host.list_issue_comments(issue_url="https://github.com/souliane/teatree/issues/7")
        argv = mock_run.call_args.args
        assert "--paginate" in argv
        assert "--slurp" in argv
        assert result == [*page_one, *page_two]
        assert {"id": 100, "body": "## Test Plan"} in result

    def test_update_issue_comment_patches_comment_endpoint(self) -> None:
        with patch.object(github_mod, "_gh_api_patch", return_value={"id": 99}) as mock_patch:
            host = GitHubCodeHost()
            result = host.update_issue_comment(
                issue_url="https://github.com/souliane/teatree/issues/7",
                comment_id=99,
                body="new",
            )
        assert result == {"id": 99}
        assert mock_patch.call_args.args[0] == "repos/souliane/teatree/issues/comments/99"

    def test_update_issue_comment_rejects_non_issue_url(self) -> None:
        host = GitHubCodeHost()
        result = host.update_issue_comment(
            issue_url="https://github.com/souliane/teatree/pull/7",
            comment_id=99,
            body="new",
        )
        assert "error" in result

    def test_upload_file_raises(self) -> None:
        host = GitHubCodeHost()
        import pytest  # noqa: PLC0415

        with pytest.raises(NotImplementedError, match="File upload"):
            host.upload_file(repo="org/repo", filepath="/tmp/test.txt")

    def test_get_issue_parses_url_and_returns_payload(self) -> None:
        payload = {"number": 7, "title": "Bug", "body": "details"}
        with patch.object(github_mod, "_gh_api_get", return_value=payload) as mock_get:
            host = GitHubCodeHost(token="tok")
            result = host.get_issue("https://github.com/souliane/teatree/issues/7")
        assert result == payload
        mock_get.assert_called_once_with("repos/souliane/teatree/issues/7", token="tok")

    def test_get_issue_rejects_non_issue_url(self) -> None:
        host = GitHubCodeHost()
        result = host.get_issue("https://github.com/souliane/teatree/pull/12")
        assert "error" in result

    def test_get_issue_returns_error_when_api_returns_non_dict(self) -> None:
        with patch.object(github_mod, "_gh_api_get", return_value=[]):
            host = GitHubCodeHost()
            result = host.get_issue("https://github.com/souliane/teatree/issues/9")
        assert "error" in result

    def test_get_review_state_returns_approved_for_latest_review(self) -> None:
        from teatree.core.backend_protocols import ReviewState  # noqa: PLC0415

        reviews = [
            {"user": {"login": "alice"}, "state": "COMMENTED"},
            {"user": {"login": "alice"}, "state": "APPROVED"},
        ]
        with patch.object(github_mod, "_gh_api_get_paginated", return_value=reviews) as mock_paginated:
            host = GitHubCodeHost(token="tok")
            result = host.get_review_state(
                pr_url="https://github.com/o/r/pull/7",
                reviewer="alice",
            )
        assert result == ReviewState.APPROVED
        mock_paginated.assert_called_once_with("repos/o/r/pulls/7/reviews?per_page=100", token="tok")

    def test_get_review_state_returns_dismissed_when_latest_is_dismissed(self) -> None:
        from teatree.core.backend_protocols import ReviewState  # noqa: PLC0415

        reviews = [
            {"user": {"login": "alice"}, "state": "APPROVED"},
            {"user": {"login": "alice"}, "state": "DISMISSED"},
        ]
        with patch.object(github_mod, "_gh_api_get_paginated", return_value=reviews):
            host = GitHubCodeHost()
            assert host.get_review_state(pr_url="https://github.com/o/r/pull/7", reviewer="alice") == (
                ReviewState.DISMISSED
            )

    def test_get_review_state_ignores_comment_only_state(self) -> None:
        from teatree.core.backend_protocols import ReviewState  # noqa: PLC0415

        # COMMENTED is not a terminal state; falls through to requested_reviewers → PENDING.
        commented = [{"user": {"login": "alice"}, "state": "COMMENTED"}]
        with (
            patch.object(github_mod, "_gh_api_get_paginated", return_value=commented),
            patch.object(github_mod, "_gh_api_get", return_value={"requested_reviewers": [{"login": "alice"}]}),
        ):
            host = GitHubCodeHost()
            result = host.get_review_state(pr_url="https://github.com/o/r/pull/7", reviewer="alice")
        assert result == ReviewState.PENDING

    def test_get_review_state_returns_pending_when_in_requested_reviewers(self) -> None:
        from teatree.core.backend_protocols import ReviewState  # noqa: PLC0415

        pr_payload = {"requested_reviewers": [{"login": "alice"}, {"login": "bob"}]}
        with (
            patch.object(github_mod, "_gh_api_get_paginated", return_value=[]),
            patch.object(github_mod, "_gh_api_get", return_value=pr_payload),
        ):
            host = GitHubCodeHost()
            assert host.get_review_state(pr_url="https://github.com/o/r/pull/7", reviewer="alice") == (
                ReviewState.PENDING
            )

    def test_get_review_state_returns_none_for_unparseable_url(self) -> None:
        from teatree.core.backend_protocols import ReviewState  # noqa: PLC0415

        host = GitHubCodeHost()
        assert host.get_review_state(pr_url="https://gitlab.com/x/-/merge_requests/1", reviewer="alice") == (
            ReviewState.NONE
        )

    def test_get_review_state_returns_none_when_no_match_anywhere(self) -> None:
        from teatree.core.backend_protocols import ReviewState  # noqa: PLC0415

        with (
            patch.object(github_mod, "_gh_api_get_paginated", return_value=[]),
            patch.object(github_mod, "_gh_api_get", return_value={"requested_reviewers": []}),
        ):
            host = GitHubCodeHost()
            assert host.get_review_state(pr_url="https://github.com/o/r/pull/7", reviewer="alice") == (ReviewState.NONE)

    def test_get_review_state_returns_none_when_reviewer_empty(self) -> None:
        from teatree.core.backend_protocols import ReviewState  # noqa: PLC0415

        host = GitHubCodeHost()
        assert host.get_review_state(pr_url="https://github.com/o/r/pull/7", reviewer="") == ReviewState.NONE

    def test_get_review_state_paginates_beyond_first_page(self) -> None:
        # GitHub returns reviews oldest-first. With >100 reviews the latest
        # terminal state lives on page 2+; a single GET misreads dismissed-then-
        # re-approved as still DISMISSED — spuriously blocking the PR sweep.
        from teatree.core.backend_protocols import ReviewState  # noqa: PLC0415

        page_one = [{"user": {"login": "alice"}, "state": "APPROVED"}] * 100
        page_two = [{"user": {"login": "alice"}, "state": "DISMISSED"}]
        slurped = json.dumps([page_one, page_two])
        with patch.object(github_api_mod, "_run_gh") as mock_run:
            mock_run.return_value = MagicMock(stdout=slurped)
            host = GitHubCodeHost()
            result = host.get_review_state(pr_url="https://github.com/o/r/pull/7", reviewer="alice")
        assert result == ReviewState.DISMISSED
        argv = mock_run.call_args_list[0].args
        assert "--paginate" in argv
        assert "--slurp" in argv

    def test_get_pr_open_state_maps_open_to_open(self) -> None:
        from teatree.core.backend_protocols import PrOpenState  # noqa: PLC0415

        with patch.object(github_mod, "_gh_api_get", return_value={"state": "open"}) as mock_get:
            host = GitHubCodeHost(token="tok")
            assert host.get_pr_open_state(pr_url="https://github.com/o/r/pull/7") == PrOpenState.OPEN
        mock_get.assert_called_once_with("repos/o/r/pulls/7", token="tok")

    def test_get_pr_open_state_maps_merged_true_to_merged(self) -> None:
        from teatree.core.backend_protocols import PrOpenState  # noqa: PLC0415

        with patch.object(github_mod, "_gh_api_get", return_value={"state": "closed", "merged": True}):
            host = GitHubCodeHost()
            assert host.get_pr_open_state(pr_url="https://github.com/o/r/pull/7") == PrOpenState.MERGED

    def test_get_pr_open_state_maps_closed_unmerged_to_closed(self) -> None:
        from teatree.core.backend_protocols import PrOpenState  # noqa: PLC0415

        with patch.object(github_mod, "_gh_api_get", return_value={"state": "closed", "merged": False}):
            host = GitHubCodeHost()
            assert host.get_pr_open_state(pr_url="https://github.com/o/r/pull/7") == PrOpenState.CLOSED

    def test_get_pr_open_state_unrecognised_payload_is_unknown(self) -> None:
        from teatree.core.backend_protocols import PrOpenState  # noqa: PLC0415

        with patch.object(github_mod, "_gh_api_get", return_value={"state": "draft"}):
            host = GitHubCodeHost()
            assert host.get_pr_open_state(pr_url="https://github.com/o/r/pull/7") == PrOpenState.UNKNOWN

    def test_get_pr_open_state_non_dict_payload_is_unknown(self) -> None:
        from teatree.core.backend_protocols import PrOpenState  # noqa: PLC0415

        with patch.object(github_mod, "_gh_api_get", return_value=["not", "a", "dict"]):
            host = GitHubCodeHost()
            assert host.get_pr_open_state(pr_url="https://github.com/o/r/pull/7") == PrOpenState.UNKNOWN

    def test_get_pr_open_state_unparsable_url_is_unknown(self) -> None:
        from teatree.core.backend_protocols import PrOpenState  # noqa: PLC0415

        with patch.object(github_mod, "_gh_api_get") as mock_get:
            host = GitHubCodeHost()
            assert host.get_pr_open_state(pr_url="https://gitlab.com/o/r/-/merge_requests/7") == PrOpenState.UNKNOWN
        mock_get.assert_not_called()

    def test_get_pr_open_state_any_exception_fails_open_to_unknown(self) -> None:
        from teatree.core.backend_protocols import PrOpenState  # noqa: PLC0415

        with patch.object(github_mod, "_gh_api_get", side_effect=RuntimeError("gh api auth failure")):
            host = GitHubCodeHost()
            assert host.get_pr_open_state(pr_url="https://github.com/o/r/pull/7") == PrOpenState.UNKNOWN

    def test_get_pr_author_returns_login(self) -> None:
        with patch.object(github_mod, "_gh_api_get", return_value={"user": {"login": "souliane"}}) as mock_get:
            host = GitHubCodeHost(token="tok")
            assert host.get_pr_author(pr_url="https://github.com/o/r/pull/7") == "souliane"
        mock_get.assert_called_once_with("repos/o/r/pulls/7", token="tok")

    def test_get_pr_author_author_less_payload_is_empty(self) -> None:
        with patch.object(github_mod, "_gh_api_get", return_value={"state": "open"}):
            host = GitHubCodeHost()
            assert host.get_pr_author(pr_url="https://github.com/o/r/pull/7") == ""

    def test_get_pr_author_non_dict_payload_is_empty(self) -> None:
        with patch.object(github_mod, "_gh_api_get", return_value=["not", "a", "dict"]):
            host = GitHubCodeHost()
            assert host.get_pr_author(pr_url="https://github.com/o/r/pull/7") == ""

    def test_get_pr_author_unparsable_url_is_empty(self) -> None:
        with patch.object(github_mod, "_gh_api_get") as mock_get:
            host = GitHubCodeHost()
            assert host.get_pr_author(pr_url="https://gitlab.com/o/r/-/merge_requests/7") == ""
        mock_get.assert_not_called()

    def test_get_pr_author_any_exception_fails_safe_to_empty(self) -> None:
        with patch.object(github_mod, "_gh_api_get", side_effect=RuntimeError("gh api auth failure")):
            host = GitHubCodeHost()
            assert host.get_pr_author(pr_url="https://github.com/o/r/pull/7") == ""


import pytest  # noqa: E402

from teatree.core.models import OutboundClaim  # noqa: E402
from teatree.loop.scanners.outbound_audit import _hash_body  # noqa: E402


# ast-grep-ignore: ac-django-no-pytest-django-db
@pytest.mark.django_db
class TestGitHubCommentOutboundClaim:
    """``post_pr_comment`` / ``post_issue_comment`` record an OutboundClaim (#1198).

    The claim row is the audit handle the outbound-audit scanner uses to
    later verify the comment really landed on GitHub. Recording is
    best-effort — a ledger outage must not break a publish that already
    succeeded.
    """

    def test_post_pr_comment_records_github_note_claim(self) -> None:
        with patch.object(
            github_mod,
            "_gh_api_post",
            return_value={"id": 42, "html_url": "https://github.com/org/repo/pull/5#issuecomment-42"},
        ):
            host = GitHubCodeHost()
            result = host.post_pr_comment(repo="org/repo", pr_iid=5, body="LGTM")
        assert result["id"] == 42
        claim = OutboundClaim.objects.get(idempotency_key="github_note:org/repo#5:42")
        assert claim.kind == OutboundClaim.Kind.GITHUB_NOTE
        assert claim.target_url == "https://github.com/org/repo/pull/5#issuecomment-42"
        assert claim.extra["repo"] == "org/repo"
        assert claim.extra["target_number"] == 5
        assert claim.extra["artifact_id"] == "42"
        assert claim.extra["payload_digest"] == _hash_body("LGTM")

    def test_post_pr_comment_idempotent_on_repeated_post(self) -> None:
        """A retried POST that the API collapsed to the same id no-ops at the ledger."""
        with patch.object(github_mod, "_gh_api_post", return_value={"id": 99}):
            host = GitHubCodeHost()
            host.post_pr_comment(repo="org/repo", pr_iid=7, body="thanks")
            host.post_pr_comment(repo="org/repo", pr_iid=7, body="thanks")
        rows = OutboundClaim.objects.filter(idempotency_key="github_note:org/repo#7:99")
        assert rows.count() == 1

    def test_post_pr_comment_without_id_records_no_claim(self) -> None:
        """A 4xx/5xx-shaped response that lacks ``id`` does not write a phantom claim."""
        with patch.object(github_mod, "_gh_api_post", return_value={"error": "boom"}):
            host = GitHubCodeHost()
            host.post_pr_comment(repo="org/repo", pr_iid=5, body="x")
        assert not OutboundClaim.objects.filter(kind=OutboundClaim.Kind.GITHUB_NOTE).exists()

    def test_post_pr_comment_non_dict_response_records_no_claim(self) -> None:
        with patch.object(github_mod, "_gh_api_post", return_value="error string"):
            host = GitHubCodeHost()
            result = host.post_pr_comment(repo="org/repo", pr_iid=5, body="x")
        assert result == {}
        assert not OutboundClaim.objects.filter(kind=OutboundClaim.Kind.GITHUB_NOTE).exists()

    def test_post_pr_comment_post_raises_records_no_claim_and_propagates(self) -> None:
        """Transport error on POST: claim never written, exception propagates to caller."""
        with patch.object(github_mod, "_gh_api_post", side_effect=RuntimeError("network down")):
            host = GitHubCodeHost()
            with pytest.raises(RuntimeError, match="network down"):
                host.post_pr_comment(repo="org/repo", pr_iid=5, body="x")
        assert not OutboundClaim.objects.filter(kind=OutboundClaim.Kind.GITHUB_NOTE).exists()

    def test_post_issue_comment_records_github_note_claim(self) -> None:
        with patch.object(
            github_mod,
            "_gh_api_post",
            return_value={"id": 77, "html_url": "https://github.com/org/repo/issues/9#issuecomment-77"},
        ):
            host = GitHubCodeHost()
            result = host.post_issue_comment(
                issue_url="https://github.com/org/repo/issues/9",
                body="ack",
            )
        assert result["id"] == 77
        claim = OutboundClaim.objects.get(idempotency_key="github_note:org/repo#9:77")
        assert claim.kind == OutboundClaim.Kind.GITHUB_NOTE
        assert claim.extra["target_number"] == 9
        assert claim.extra["payload_digest"] == _hash_body("ack")

    def test_post_issue_comment_rejects_non_issue_url_records_no_claim(self) -> None:
        with patch.object(github_mod, "_gh_api_post") as mock_post:
            host = GitHubCodeHost()
            result = host.post_issue_comment(
                issue_url="https://github.com/org/repo/pull/9",
                body="ack",
            )
        assert "error" in result
        mock_post.assert_not_called()
        assert not OutboundClaim.objects.filter(kind=OutboundClaim.Kind.GITHUB_NOTE).exists()

    def test_record_failure_does_not_break_publish_path(self) -> None:
        """Even if claim recording somehow raises, the publish's return value is unchanged."""
        with (
            patch.object(github_mod, "_gh_api_post", return_value={"id": 5}),
            patch(
                "teatree.core.models.OutboundClaim.objects",
                new_callable=lambda: MagicMock(get_or_create=MagicMock(side_effect=RuntimeError("DB down"))),
            ),
        ):
            host = GitHubCodeHost()
            # The publish succeeded — the swallow happens inside _record_github_note_claim.
            result = host.post_pr_comment(repo="org/repo", pr_iid=1, body="hi")
        assert result == {"id": 5}


class TestGitHubWave2Reads:
    """Wave-2 forge READ methods — pr list/diff/commits + repo metadata."""

    def test_list_prs_builds_repo_state_author_search(self) -> None:
        with patch.object(github_mod, "_gh_api_search_paginated", return_value=[{"number": 5}]) as mock_search:
            result = GitHubCodeHost(token="tok").list_prs(repo="org/repo", state="open", author="alice")
        assert result == [{"number": 5}]
        mock_search.assert_called_once_with(
            "search/issues?q=repo%3Aorg%2Frepo+is%3Apr+is%3Aopen+author%3Aalice&per_page=100",
            token="tok",
        )

    def test_list_prs_omits_empty_state_and_author(self) -> None:
        with patch.object(github_mod, "_gh_api_search_paginated", return_value=[]) as mock_search:
            GitHubCodeHost().list_prs(repo="org/repo")
        mock_search.assert_called_once_with(
            "search/issues?q=repo%3Aorg%2Frepo+is%3Apr&per_page=100",
            token="",
        )

    def test_get_pr_diff_returns_changed_files(self) -> None:
        files = [{"filename": "a.py", "additions": 3, "patch": "@@"}]
        with patch.object(github_mod, "_gh_api_get_paginated", return_value=files) as mock_get:
            result = GitHubCodeHost(token="tok").get_pr_diff(repo="org/repo", pr_iid=42)
        assert result == files
        mock_get.assert_called_once_with("repos/org/repo/pulls/42/files?per_page=100", token="tok")

    def test_get_pr_diff_unknown_pr_returns_empty(self) -> None:
        with patch.object(
            github_mod,
            "_gh_api_get_paginated",
            side_effect=utils_run_mod.CommandFailedError(["gh"], 1, "", "gh: Not Found (HTTP 404)"),
        ):
            assert GitHubCodeHost().get_pr_diff(repo="org/repo", pr_iid=99) == []

    def test_get_pr_diff_reraises_non_404_error(self) -> None:
        # F8.5: an auth/rate-limit/network failure must NOT read as an empty diff —
        # a reviewer signing off on "this PR touches nothing" would be a lie.
        with (
            patch.object(
                github_mod,
                "_gh_api_get_paginated",
                side_effect=utils_run_mod.CommandFailedError(["gh"], 1, "", "gh: Bad credentials (HTTP 401)"),
            ),
            pytest.raises(utils_run_mod.CommandFailedError),
        ):
            GitHubCodeHost().get_pr_diff(repo="org/repo", pr_iid=99)

    def test_list_pr_commits_returns_commits(self) -> None:
        commits = [{"sha": "abc", "commit": {"message": "fix"}}]
        with patch.object(github_mod, "_gh_api_get_paginated", return_value=commits) as mock_get:
            result = GitHubCodeHost().list_pr_commits(repo="org/repo", pr_iid=7)
        assert result == commits
        mock_get.assert_called_once_with("repos/org/repo/pulls/7/commits?per_page=100", token="")

    def test_list_pr_commits_unknown_pr_returns_empty(self) -> None:
        with patch.object(
            github_mod,
            "_gh_api_get_paginated",
            side_effect=utils_run_mod.CommandFailedError(["gh"], 1, "", "gh: Not Found (HTTP 404)"),
        ):
            assert GitHubCodeHost().list_pr_commits(repo="org/repo", pr_iid=99) == []

    def test_list_pr_commits_reraises_non_404_error(self) -> None:
        with (
            patch.object(
                github_mod,
                "_gh_api_get_paginated",
                side_effect=utils_run_mod.CommandFailedError(["gh"], 1, "", "gh: rate limit exceeded (HTTP 403)"),
            ),
            pytest.raises(utils_run_mod.CommandFailedError),
        ):
            GitHubCodeHost().list_pr_commits(repo="org/repo", pr_iid=99)

    def test_get_repo_returns_metadata(self) -> None:
        payload = {"default_branch": "main", "full_name": "org/repo"}
        with patch.object(github_mod, "_gh_api_get", return_value=payload) as mock_get:
            result = GitHubCodeHost(token="tok").get_repo(repo="org/repo")
        assert result == payload
        mock_get.assert_called_once_with("repos/org/repo", token="tok")

    def test_get_repo_unknown_repo_returns_structured_error(self) -> None:
        with patch.object(
            github_mod, "_gh_api_get", side_effect=utils_run_mod.CommandFailedError(["gh"], 1, "", "404")
        ):
            assert GitHubCodeHost().get_repo(repo="org/missing") == {"error": "Could not resolve repo: org/missing"}

    def test_get_repo_non_dict_payload_is_structured_error(self) -> None:
        with patch.object(github_mod, "_gh_api_get", return_value=["unexpected"]):
            assert GitHubCodeHost().get_repo(repo="org/repo") == {"error": "Repo not found: org/repo"}


def _completed(stdout: str, *, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout, "")


class TestRollupState:
    """F5.1 — aggregate a GitHub statusCheckRollup list into one my_prs word."""

    def test_empty_or_non_list_is_blank(self) -> None:
        assert github_mod._rollup_state([]) == ""
        assert github_mod._rollup_state(None) == ""

    def test_all_success_is_success(self) -> None:
        rollup = [
            {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"},
            {"__typename": "StatusContext", "state": "SUCCESS"},
        ]
        assert github_mod._rollup_state(rollup) == "success"

    def test_any_failure_dominates(self) -> None:
        rollup = [
            {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"},
            {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "FAILURE"},
        ]
        assert github_mod._rollup_state(rollup) == "failure"

    def test_in_progress_without_failure_is_pending(self) -> None:
        rollup = [
            {"__typename": "CheckRun", "status": "IN_PROGRESS", "conclusion": None},
            {"__typename": "StatusContext", "state": "SUCCESS"},
        ]
        assert github_mod._rollup_state(rollup) == "pending"

    def test_neutral_and_skipped_count_as_passing(self) -> None:
        rollup = [
            {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "NEUTRAL"},
            {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SKIPPED"},
        ]
        assert github_mod._rollup_state(rollup) == "success"


class TestListMyPrsEnrichment:
    """F5.1 — list_my_prs enriches each search hit with head SHA + CI rollup."""

    def _search_hit(self, number: int = 9) -> dict[str, object]:
        return {"number": number, "title": "Fix", "html_url": f"https://github.com/o/r/pull/{number}"}

    def test_enriches_hit_with_head_sha_and_rollup(self) -> None:
        detail = json.dumps(
            {
                "headRefOid": "cafef00d",
                "statusCheckRollup": [{"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "FAILURE"}],
                "mergeable": "MERGEABLE",
                "mergeStateStatus": "BLOCKED",
            }
        )
        with (
            patch.object(github_mod, "_gh_api_search_paginated", return_value=[self._search_hit()]),
            patch.object(github_mod, "_run_gh", return_value=_completed(detail)) as mock_run,
        ):
            prs = GitHubCodeHost(token="tok").list_my_prs(author="alice")
        assert prs[0]["sha"] == "cafef00d"
        assert prs[0]["status_check_rollup"] == {"state": "failure"}
        # The enrichment read is timeout-bounded.
        assert mock_run.call_args.kwargs["timeout"] == _FORGE_READ_TIMEOUT_SECONDS

    def test_enriched_pr_drives_the_failed_lane_via_scanner(self) -> None:
        from teatree.loop.scanners.my_prs import MyPrsScanner  # noqa: PLC0415

        detail = json.dumps(
            {"headRefOid": "abc", "statusCheckRollup": [{"__typename": "StatusContext", "state": "FAILURE"}]}
        )

        class _Host:
            def current_user(self) -> str:
                return "alice"

            def list_my_prs(self, *, author: str, updated_after: str | None = None):
                del updated_after
                hit = {"number": 9, "title": "x", "html_url": "https://github.com/o/r/pull/9"}
                with (
                    patch.object(github_mod, "_gh_api_search_paginated", return_value=[hit]),
                    patch.object(github_mod, "_run_gh", return_value=_completed(detail)),
                ):
                    return GitHubCodeHost().list_my_prs(author=author)

        signals = MyPrsScanner(host=_Host()).scan()
        assert [s.kind for s in signals] == ["my_pr.failed"]

    def test_enrichment_failure_leaves_hit_unenriched(self) -> None:
        with (
            patch.object(github_mod, "_gh_api_search_paginated", return_value=[self._search_hit()]),
            patch.object(
                github_mod, "_run_gh", side_effect=utils_run_mod.CommandFailedError(["gh"], 1, "", "boom")
            ),
        ):
            prs = GitHubCodeHost().list_my_prs(author="alice")
        # Unenriched — no fabricated pipeline field, so the scanner surfaces the gap.
        assert "status_check_rollup" not in prs[0]
        assert "sha" not in prs[0]

    def test_unparseable_html_url_is_left_unenriched(self) -> None:
        with (
            patch.object(github_mod, "_gh_api_search_paginated", return_value=[{"number": 1, "html_url": "not-a-url"}]),
            patch.object(github_mod, "_run_gh") as mock_run,
        ):
            prs = GitHubCodeHost().list_my_prs(author="alice")
        mock_run.assert_not_called()
        assert "status_check_rollup" not in prs[0]


class TestListMyMergedPrsCap:
    """F8.8 — an uncut merged-PR search warns about the 1000-result cap."""

    def test_warns_without_cutoff(self, caplog) -> None:
        from teatree.utils.throttled_log import reset_throttle  # noqa: PLC0415

        reset_throttle()
        with (
            patch.object(github_mod, "_gh_api_search_paginated", return_value=[]),
            caplog.at_level(logging.WARNING, logger="teatree.backends.github.client"),
        ):
            GitHubCodeHost().list_my_merged_prs(author="alice")
        assert any("caps at 1000" in r.message for r in caplog.records)

    def test_no_warning_with_cutoff(self, caplog) -> None:
        from teatree.utils.throttled_log import reset_throttle  # noqa: PLC0415

        reset_throttle()
        with (
            patch.object(github_mod, "_gh_api_search_paginated", return_value=[]),
            caplog.at_level(logging.WARNING, logger="teatree.backends.github.client"),
        ):
            GitHubCodeHost().list_my_merged_prs(author="alice", updated_after="2026-01-01T00:00:00Z")
        assert not any("caps at 1000" in r.message for r in caplog.records)


class TestDirectRunGhTimeouts:
    """F8.4 — direct _run_gh calls on the host bound their subprocess."""

    def test_is_assignable_bounds_timeout(self) -> None:
        with (
            patch("teatree.utils.git.remote_slug", return_value="o/r"),
            patch.object(github_mod, "_run_gh", return_value=_completed("")) as mock_run,
        ):
            GitHubCodeHost(token="t").is_assignable(repo="o/r", login="alice")
        assert mock_run.call_args.kwargs["timeout"] == _FORGE_READ_TIMEOUT_SECONDS

    def test_delete_issue_comment_bounds_timeout(self) -> None:
        with patch.object(github_mod, "_run_gh", return_value=_completed("")) as mock_run:
            GitHubCodeHost().delete_issue_comment(
                issue_url="https://github.com/o/r/issues/3", comment_id=5
            )
        assert mock_run.call_args.kwargs["timeout"] == _FORGE_READ_TIMEOUT_SECONDS


def test_logging_import_present() -> None:
    # `logging` is imported at module top for the caplog-based tests above.
    assert logging.getLogger("teatree.backends.github.client") is not None
