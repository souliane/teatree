"""Tests for teatree.backends.github — GitHub API helpers and GitHubCodeHost."""

import json
import subprocess
from unittest.mock import MagicMock, patch

import teatree.backends.github as github_mod
import teatree.utils.run as utils_run_mod
from teatree.backends.github import (
    GitHubCodeHost,
    ProjectItem,
    _gh_api_get,
    _gh_api_patch,
    _gh_api_post,
    _gh_graphql,
    _run_gh,
    fetch_project_items,
)
from teatree.backends.protocols import PullRequestSpec


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


class TestGhApiGet:
    def test_returns_parsed_json(self) -> None:
        with patch.object(github_mod, "_run_gh") as mock_run:
            mock_run.return_value = MagicMock(stdout='{"key": "value"}')
            result = _gh_api_get("/repos/test/issues")
        assert result == {"key": "value"}

    def test_passes_token(self) -> None:
        with patch.object(github_mod, "_run_gh") as mock_run:
            mock_run.return_value = MagicMock(stdout="{}")
            _gh_api_get("/test", token="tok")
        assert mock_run.call_args[1]["token"] == "tok"


class TestGhApiPost:
    def test_sends_payload_via_stdin(self) -> None:
        with patch.object(utils_run_mod.subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 0, '{"id": 1}', "")
            result = _gh_api_post("/test", {"body": "hello"})
        assert result == {"id": 1}
        call_kwargs = mock_run.call_args[1]
        assert json.loads(call_kwargs["input"]) == {"body": "hello"}

    def test_includes_token(self) -> None:
        with patch.object(utils_run_mod.subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 0, "{}", "")
            _gh_api_post("/test", {}, token="tok")
        args = mock_run.call_args[0][0]
        assert "Authorization: Bearer tok" in args


class TestGhApiPatch:
    def test_sends_patch_request(self) -> None:
        with patch.object(utils_run_mod.subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 0, '{"updated": true}', "")
            result = _gh_api_patch("/test/1", {"title": "new"})
        assert result == {"updated": True}
        args = mock_run.call_args[0][0]
        assert "--method" in args
        assert "PATCH" in args

    def test_includes_token(self) -> None:
        with patch.object(utils_run_mod.subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 0, "{}", "")
            _gh_api_patch("/test", {}, token="tok")
        args = mock_run.call_args[0][0]
        assert "Authorization: Bearer tok" in args


class TestGhGraphql:
    def test_executes_query(self) -> None:
        with patch.object(utils_run_mod.subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 0, '{"data": {}}', "")
            result = _gh_graphql("{ viewer { login } }")
        assert result == {"data": {}}
        args = mock_run.call_args[0][0]
        assert "graphql" in args

    def test_includes_token(self) -> None:
        with patch.object(utils_run_mod.subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 0, "{}", "")
            _gh_graphql("{ test }", token="tok")
        args = mock_run.call_args[0][0]
        assert "Authorization: Bearer tok" in args


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
        with patch.object(github_mod, "_gh_graphql", return_value=graphql_response):
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
        with patch.object(github_mod, "_gh_graphql", return_value={"data": {"user": {}}}):
            items = fetch_project_items("testuser", 1)
        assert items == []

    def test_skips_non_dict_nodes(self) -> None:
        graphql_response = {"data": {"user": {"projectV2": {"items": {"nodes": [None, "invalid"]}}}}}
        with patch.object(github_mod, "_gh_graphql", return_value=graphql_response):
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
        with patch.object(github_mod, "_gh_graphql", return_value=graphql_response):
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
        with patch.object(github_mod, "_gh_graphql", return_value=graphql_response):
            items = fetch_project_items("testuser", 1)
        assert len(items) == 1
        assert items[0].status == ""


class TestGitHubCodeHost:
    def test_create_pr(self) -> None:
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
        assert result == {"url": "https://github.com/org/repo/pull/1"}

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

    def test_list_open_prs(self) -> None:
        prs = [
            {"number": 1, "user": {"login": "alice"}},
            {"number": 2, "user": {"login": "bob"}},
        ]
        with patch.object(github_mod, "_gh_api_get", return_value=prs):
            host = GitHubCodeHost()
            result = host.list_open_prs("org/repo", "alice")
        assert len(result) == 1
        assert result[0]["number"] == 1

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

    def test_list_open_prs_returns_empty_for_non_list(self) -> None:
        with patch.object(github_mod, "_gh_api_get", return_value={"error": "bad"}):
            host = GitHubCodeHost()
            result = host.list_open_prs("org/repo", "alice")
        assert result == []

    def test_list_my_open_prs_searches_by_author_across_forge(self) -> None:
        search_response = {
            "items": [
                {"number": 1, "title": "first", "html_url": "https://github.com/org/repo/pull/1"},
                {"number": 2, "title": "second", "html_url": "https://github.com/org/other/pull/2"},
            ],
        }
        with patch.object(github_mod, "_gh_api_get", return_value=search_response) as mock_get:
            host = GitHubCodeHost(token="tok")
            result = host.list_my_open_prs("alice")
        assert len(result) == 2
        assert result[0]["number"] == 1
        mock_get.assert_called_once_with(
            "search/issues?q=is%3Apr+is%3Aopen+author%3Aalice&per_page=100",
            token="tok",
        )

    def test_list_my_open_prs_returns_empty_when_response_missing_items(self) -> None:
        with patch.object(github_mod, "_gh_api_get", return_value={"total_count": 0}):
            host = GitHubCodeHost()
            assert host.list_my_open_prs("alice") == []

    def test_list_my_open_prs_returns_empty_when_response_not_dict(self) -> None:
        with patch.object(github_mod, "_gh_api_get", return_value=[]):
            host = GitHubCodeHost()
            assert host.list_my_open_prs("alice") == []

    def test_post_mr_note(self) -> None:
        with patch.object(github_mod, "_gh_api_post", return_value={"id": 42}) as mock_post:
            host = GitHubCodeHost()
            result = host.post_mr_note(repo="org/repo", mr_iid=5, body="LGTM")
        assert result == {"id": 42}
        mock_post.assert_called_once()

    def test_post_mr_note_returns_empty_for_non_dict(self) -> None:
        with patch.object(github_mod, "_gh_api_post", return_value="error"):
            host = GitHubCodeHost()
            result = host.post_mr_note(repo="org/repo", mr_iid=5, body="test")
        assert result == {}

    def test_update_mr_note(self) -> None:
        with patch.object(github_mod, "_gh_api_patch", return_value={"id": 42}) as mock_patch:
            host = GitHubCodeHost()
            result = host.update_mr_note(repo="org/repo", mr_iid=5, note_id=42, body="Updated")
        assert result == {"id": 42}
        # Should use the note_id, not mr_iid for GitHub
        mock_patch.assert_called_once_with(
            "repos/org/repo/issues/comments/42",
            {"body": "Updated"},
            token="",
        )

    def test_update_mr_note_returns_empty_for_non_dict(self) -> None:
        with patch.object(github_mod, "_gh_api_patch", return_value=[]):
            host = GitHubCodeHost()
            result = host.update_mr_note(repo="org/repo", mr_iid=5, note_id=42, body="x")
        assert result == {}

    def test_list_mr_notes(self) -> None:
        notes = [{"id": 1, "body": "comment"}]
        with patch.object(github_mod, "_gh_api_get", return_value=notes):
            host = GitHubCodeHost()
            result = host.list_mr_notes(repo="org/repo", mr_iid=5)
        assert result == notes

    def test_list_mr_notes_returns_empty_for_non_list(self) -> None:
        with patch.object(github_mod, "_gh_api_get", return_value={"error": "bad"}):
            host = GitHubCodeHost()
            result = host.list_mr_notes(repo="org/repo", mr_iid=5)
        assert result == []

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
