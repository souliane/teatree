"""Tests for lib.gitlab — GitLab API helpers."""

from unittest.mock import MagicMock, patch

import lib.gitlab as gl
import pytest
from lib.gitlab import (
    ProjectInfo,
    _api_get,
    _api_post,
    _api_put,
    _token,
    cancel_pipelines,
    create_mr,
    current_branch,
    current_user,
    download_file,
    get_issue,
    get_issue_comments,
    get_issue_labels,
    get_mr,
    get_mr_approvals,
    get_mr_notes,
    get_mr_pipeline,
    get_mr_state,
    list_open_mrs,
    resolve_project,
    resolve_project_from_remote,
    update_issue_labels,
)

_TOK = "test-token"

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_caches() -> None:
    """Clear module-level caches between tests."""
    gl._token_cache = ""
    gl._project_cache.clear()


# ---------------------------------------------------------------------------
# _token
# ---------------------------------------------------------------------------


class TestToken:
    def test_returns_cached(self) -> None:
        gl._token_cache = _TOK
        assert _token() == _TOK

    def test_from_pass(self) -> None:
        with patch("lib.gitlab.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="pass-token\n")
            assert _token() == "pass-token"

    def test_from_glab(self) -> None:
        with patch("lib.gitlab.subprocess.run") as mock_run:

            def side_effect(args: list[str], **_kw: object) -> MagicMock:
                if "pass" in args:
                    return MagicMock(returncode=1, stdout="")
                return MagicMock(returncode=0, stdout="glab-token\n")

            mock_run.side_effect = side_effect
            assert _token() == "glab-token"

    def test_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GITLAB_TOKEN", "env-token")
        with patch("lib.gitlab.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            assert _token() == "env-token"

    def test_no_token(self) -> None:
        with patch("lib.gitlab.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            assert _token() == ""


# ---------------------------------------------------------------------------
# _api_get / _api_post / _api_put
# ---------------------------------------------------------------------------


class TestApiGet:
    def test_success(self) -> None:
        with patch("lib.gitlab.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout='{"id": 1}')
            result = _api_get("projects/1", token=_TOK)
        assert result == {"id": 1}

    def test_no_token(self) -> None:
        gl._token_cache = ""
        with patch("lib.gitlab.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            assert _api_get("projects/1") is None

    def test_curl_failure(self) -> None:
        with patch("lib.gitlab.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            assert _api_get("projects/1", token=_TOK) is None

    def test_empty_stdout(self) -> None:
        with patch("lib.gitlab.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="  ")
            assert _api_get("projects/1", token=_TOK) is None


class TestApiPost:
    def test_success_with_data(self) -> None:
        with patch("lib.gitlab.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout='{"iid": 1}')
            result = _api_post("projects/1/merge_requests", {"title": "t"}, token=_TOK)
        assert result == {"iid": 1}
        cmd = mock_run.call_args[0][0]
        assert "--data" in cmd

    def test_success_no_data(self) -> None:
        with patch("lib.gitlab.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout='{"id": 1}')
            result = _api_post("endpoint", token=_TOK)
        assert result is not None

    def test_no_token(self) -> None:
        gl._token_cache = ""
        with patch("lib.gitlab.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            assert _api_post("endpoint") is None

    def test_failure(self) -> None:
        with patch("lib.gitlab.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            assert _api_post("endpoint", token=_TOK) is None


class TestApiPut:
    def test_success_with_data(self) -> None:
        with patch("lib.gitlab.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout='{"id": 1}')
            result = _api_put("endpoint", {"key": "val"}, token=_TOK)
        assert result is not None
        cmd = mock_run.call_args[0][0]
        assert "--data" in cmd

    def test_success_no_data(self) -> None:
        with patch("lib.gitlab.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout='{"id": 1}')
            result = _api_put("endpoint", token=_TOK)
        assert result is not None

    def test_no_token(self) -> None:
        gl._token_cache = ""
        with patch("lib.gitlab.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            assert _api_put("endpoint") is None

    def test_failure(self) -> None:
        with patch("lib.gitlab.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            assert _api_put("endpoint", token=_TOK) is None


# ---------------------------------------------------------------------------
# current_user
# ---------------------------------------------------------------------------


class TestCurrentUser:
    def test_parses_username(self) -> None:
        with patch("lib.gitlab.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="",
                stderr="Logged in to gitlab.com as alice (token)",
            )
            assert current_user() == "alice"

    def test_no_match(self) -> None:
        with patch("lib.gitlab.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=1,
                stdout="some irrelevant output",
                stderr="No token found\nPlease authenticate",
            )
            assert current_user() == ""

    def test_logged_in_from_stdout(self) -> None:
        with patch("lib.gitlab.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="Logged in to gitlab.com as bob (tok)",
                stderr="",
            )
            assert current_user() == "bob"


# ---------------------------------------------------------------------------
# resolve_project / resolve_project_from_remote
# ---------------------------------------------------------------------------


class TestResolveProject:
    def test_success(self) -> None:
        api_data = {"id": 42, "path_with_namespace": "org/repo", "path": "repo"}
        with patch("lib.gitlab._api_get", return_value=api_data):
            proj = resolve_project("org/repo", token=_TOK)
        assert proj is not None
        assert proj.project_id == 42
        assert proj.short_name == "repo"

    def test_cached(self) -> None:
        cached = ProjectInfo(1, "o/r", "r")
        gl._project_cache["o/r"] = cached
        assert resolve_project("o/r") is cached

    def test_api_returns_none(self) -> None:
        with patch("lib.gitlab._api_get", return_value=None):
            assert resolve_project("org/repo", token=_TOK) is None

    def test_api_returns_list(self) -> None:
        with patch("lib.gitlab._api_get", return_value=[]):
            assert resolve_project("org/repo", token=_TOK) is None


class TestResolveProjectFromRemote:
    def test_ssh_remote(self) -> None:
        proj = ProjectInfo(42, "org/repo", "repo")
        with (
            patch("lib.gitlab.subprocess.run") as mock_run,
            patch("lib.gitlab.resolve_project", return_value=proj),
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="git@gitlab.com:org/repo.git\n")
            result = resolve_project_from_remote("/repo")
        assert result is proj

    def test_https_remote(self) -> None:
        proj = ProjectInfo(42, "org/repo", "repo")
        with (
            patch("lib.gitlab.subprocess.run") as mock_run,
            patch("lib.gitlab.resolve_project", return_value=proj),
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="https://gitlab.com/org/repo.git\n")
            result = resolve_project_from_remote("/repo")
        assert result is proj

    def test_git_failure(self) -> None:
        with patch("lib.gitlab.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            assert resolve_project_from_remote("/repo") is None

    def test_non_gitlab_remote(self) -> None:
        with patch("lib.gitlab.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="https://github.com/org/repo.git\n")
            assert resolve_project_from_remote("/repo") is None


# ---------------------------------------------------------------------------
# MR operations
# ---------------------------------------------------------------------------


class TestListOpenMrs:
    def test_success(self) -> None:
        proj = ProjectInfo(42, "org/repo", "repo")
        mrs = [{"iid": 1, "draft": False}, {"iid": 2, "draft": True}]
        with (
            patch("lib.gitlab.resolve_project", return_value=proj),
            patch("lib.gitlab._api_get", return_value=mrs),
        ):
            result = list_open_mrs("org/repo", "alice", token=_TOK)
        assert len(result) == 2

    def test_exclude_draft(self) -> None:
        proj = ProjectInfo(42, "org/repo", "repo")
        mrs = [{"iid": 1, "draft": False}, {"iid": 2, "draft": True}]
        with (
            patch("lib.gitlab.resolve_project", return_value=proj),
            patch("lib.gitlab._api_get", return_value=mrs),
        ):
            result = list_open_mrs("org/repo", "alice", token=_TOK, include_draft=False)
        assert len(result) == 1

    def test_no_project(self) -> None:
        with patch("lib.gitlab.resolve_project", return_value=None):
            assert list_open_mrs("bad/repo", "alice") == []

    def test_api_returns_none(self) -> None:
        proj = ProjectInfo(42, "org/repo", "repo")
        with (
            patch("lib.gitlab.resolve_project", return_value=proj),
            patch("lib.gitlab._api_get", return_value=None),
        ):
            assert list_open_mrs("org/repo", "alice", token=_TOK) == []

    def test_api_returns_dict(self) -> None:
        proj = ProjectInfo(42, "org/repo", "repo")
        with (
            patch("lib.gitlab.resolve_project", return_value=proj),
            patch("lib.gitlab._api_get", return_value={"error": "bad"}),
        ):
            assert list_open_mrs("org/repo", "alice", token=_TOK) == []


class TestGetMr:
    def test_success(self) -> None:
        with patch("lib.gitlab._api_get", return_value={"iid": 1, "state": "opened"}):
            result = get_mr(42, 1, token=_TOK)
        assert result is not None
        assert result["iid"] == 1

    def test_none(self) -> None:
        with patch("lib.gitlab._api_get", return_value=None):
            assert get_mr(42, 1, token=_TOK) is None

    def test_list_response(self) -> None:
        with patch("lib.gitlab._api_get", return_value=[]):
            assert get_mr(42, 1, token=_TOK) is None


class TestGetMrApprovals:
    def test_with_approvals(self) -> None:
        data = {
            "approved_by": [{"user": {"username": "alice"}}],
            "approvals_required": 1,
        }
        with patch("lib.gitlab._api_get", return_value=data):
            result = get_mr_approvals(42, 1, token=_TOK)
        assert result["count"] == 1
        assert result["required"] == 1
        assert result["approved_by"] == ["alice"]

    def test_no_data(self) -> None:
        with patch("lib.gitlab._api_get", return_value=None):
            result = get_mr_approvals(42, 1, token=_TOK)
        assert result == {"count": 0, "required": 0, "approved_by": []}


class TestGetMrNotes:
    def test_filters_system_and_author(self) -> None:
        notes = [
            {"body": "comment", "system": False, "author": {"username": "bob"}},
            {"body": "system note", "system": True, "author": {"username": "bot"}},
            {"body": "self comment", "system": False, "author": {"username": "alice"}},
        ]
        with patch("lib.gitlab._api_get", return_value=notes):
            result = get_mr_notes(42, 1, token=_TOK, exclude_author="alice")
        assert len(result) == 1
        assert result[0]["body"] == "comment"

    def test_no_filtering(self) -> None:
        notes = [
            {"body": "a", "system": True, "author": {"username": "x"}},
            {"body": "b", "system": False, "author": {"username": "y"}},
        ]
        with patch("lib.gitlab._api_get", return_value=notes):
            result = get_mr_notes(42, 1, token=_TOK, exclude_system=False, exclude_author="")
        assert len(result) == 2

    def test_no_data(self) -> None:
        with patch("lib.gitlab._api_get", return_value=None):
            assert get_mr_notes(42, 1, token=_TOK) == []


class TestGetMrPipeline:
    def test_with_pipeline(self) -> None:
        data = {"head_pipeline": {"status": "success", "web_url": "https://pipe"}}
        with patch("lib.gitlab._api_get", return_value=data):
            result = get_mr_pipeline(42, 1, token=_TOK)
        assert result == {"status": "success", "url": "https://pipe"}

    def test_no_pipeline(self) -> None:
        data = {"head_pipeline": None}
        with patch("lib.gitlab._api_get", return_value=data):
            result = get_mr_pipeline(42, 1, token=_TOK)
        assert result == {"status": None, "url": None}

    def test_no_data(self) -> None:
        with patch("lib.gitlab._api_get", return_value=None):
            result = get_mr_pipeline(42, 1, token=_TOK)
        assert result == {"status": None, "url": None}


class TestGetMrState:
    def test_success(self) -> None:
        with patch("lib.gitlab._api_get", return_value={"state": "merged", "merge_commit_sha": "abc"}):
            result = get_mr_state(42, 1, token=_TOK)
        assert result is not None
        assert result["state"] == "merged"

    def test_no_data(self) -> None:
        with patch("lib.gitlab._api_get", return_value=None):
            assert get_mr_state(42, 1, token=_TOK) is None


class TestCreateMr:
    def test_with_assignee_and_description(self) -> None:
        with (
            patch("lib.gitlab._api_get", return_value=[{"id": 99}]),
            patch("lib.gitlab._api_post", return_value={"iid": 1}) as mock_post,
        ):
            result = create_mr(
                42,
                "feat",
                "main",
                "title",
                "desc",
                assignee_username="alice",
                token=_TOK,
            )
        assert result == {"iid": 1}
        payload = mock_post.call_args[0][1]
        assert payload["assignee_id"] == 99
        assert payload["description"] == "desc"

    def test_no_assignee(self) -> None:
        with patch("lib.gitlab._api_post", return_value={"iid": 2}) as mock_post:
            result = create_mr(42, "feat", "main", "title", token=_TOK)
        assert result is not None
        payload = mock_post.call_args[0][1]
        assert "assignee_id" not in payload

    def test_user_lookup_fails(self) -> None:
        with (
            patch("lib.gitlab._api_get", return_value=None),
            patch("lib.gitlab._api_post", return_value={"iid": 3}) as mock_post,
        ):
            result = create_mr(
                42,
                "feat",
                "main",
                "title",
                assignee_username="alice",
                token=_TOK,
            )
        assert result is not None
        payload = mock_post.call_args[0][1]
        assert "assignee_id" not in payload


# ---------------------------------------------------------------------------
# Pipeline operations
# ---------------------------------------------------------------------------


class TestCancelPipelines:
    def test_cancels(self) -> None:
        pipelines = [{"id": 100}, {"id": 200}]
        with (
            patch("lib.gitlab._api_get", return_value=pipelines),
            patch("lib.gitlab._api_post") as mock_post,
        ):
            result = cancel_pipelines(42, "feat", token=_TOK, statuses=("running",))
        assert result == [100, 200]
        assert mock_post.call_count == 2

    def test_no_data(self) -> None:
        with patch("lib.gitlab._api_get", return_value=None):
            assert cancel_pipelines(42, "feat", token=_TOK) == []

    def test_pipeline_no_id(self) -> None:
        with (
            patch("lib.gitlab._api_get", return_value=[{}]),
            patch("lib.gitlab._api_post") as mock_post,
        ):
            cancel_pipelines(42, "feat", token=_TOK, statuses=("running",))
        mock_post.assert_not_called()


# ---------------------------------------------------------------------------
# Issue operations
# ---------------------------------------------------------------------------


class TestGetIssue:
    def test_success(self) -> None:
        with patch("lib.gitlab._api_get", return_value={"iid": 1, "title": "Bug"}):
            result = get_issue(42, 1, token=_TOK)
        assert result is not None
        assert result["title"] == "Bug"

    def test_none(self) -> None:
        with patch("lib.gitlab._api_get", return_value=None):
            assert get_issue(42, 1, token=_TOK) is None

    def test_list_response(self) -> None:
        with patch("lib.gitlab._api_get", return_value=[]):
            assert get_issue(42, 1, token=_TOK) is None


class TestGetIssueLabels:
    def test_with_labels(self) -> None:
        with patch("lib.gitlab.get_issue", return_value={"labels": ["bug", "urgent"]}):
            assert get_issue_labels(42, 1) == ["bug", "urgent"]

    def test_no_issue(self) -> None:
        with patch("lib.gitlab.get_issue", return_value=None):
            assert get_issue_labels(42, 1) == []


class TestGetIssueComments:
    def test_filters_system(self) -> None:
        notes = [
            {"body": "user comment", "system": False},
            {"body": "system note", "system": True},
        ]
        with patch("lib.gitlab._api_get", return_value=notes):
            result = get_issue_comments(42, 1, token=_TOK)
        assert len(result) == 1
        assert result[0]["body"] == "user comment"

    def test_no_data(self) -> None:
        with patch("lib.gitlab._api_get", return_value=None):
            assert get_issue_comments(42, 1, token=_TOK) == []


class TestUpdateIssueLabels:
    def test_add_and_remove(self) -> None:
        with patch("lib.gitlab._api_put", return_value={"id": 1}) as mock_put:
            result = update_issue_labels(
                42,
                1,
                add_labels=["bug"],
                remove_labels=["feature"],
                token=_TOK,
            )
        assert result is not None
        payload = mock_put.call_args[0][1]
        assert payload["add_labels"] == "bug"
        assert payload["remove_labels"] == "feature"

    def test_no_labels(self) -> None:
        assert update_issue_labels(42, 1) is None


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


class TestDownloadFile:
    def test_success_with_token(self) -> None:
        gl._token_cache = _TOK
        with patch("lib.gitlab.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            assert download_file("https://example.com/file", "/tmp/f")
        cmd = mock_run.call_args[0][0]
        assert f"PRIVATE-TOKEN: {_TOK}" in cmd

    def test_failure(self) -> None:
        gl._token_cache = _TOK
        with patch("lib.gitlab.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1)
            assert not download_file("https://example.com/file", "/tmp/f")

    def test_no_token(self) -> None:
        with (
            patch("lib.gitlab._token", return_value=""),
            patch("lib.gitlab.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            download_file("https://example.com/file", "/tmp/f")
        cmd = mock_run.call_args[0][0]
        assert all("PRIVATE-TOKEN" not in str(c) for c in cmd)


class TestCurrentBranch:
    def test_success(self) -> None:
        with patch("lib.gitlab.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="feat-branch\n")
            assert current_branch("/repo") == "feat-branch"

    def test_failure(self) -> None:
        with patch("lib.gitlab.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            assert current_branch("/repo") == ""


class TestDiscoverMrs:
    def test_discovers_across_repos(self) -> None:
        proj = ProjectInfo(project_id=1, path_with_namespace="org/repo", short_name="repo")
        mr = {"iid": 10, "title": "fix"}
        with (
            patch("lib.gitlab.resolve_project", return_value=proj),
            patch("lib.gitlab.list_open_mrs", return_value=[mr]),
        ):
            result = gl.discover_mrs(["org/repo"], "alice")
        assert len(result) == 1
        assert result[0]["_repo_short"] == "repo"
        assert result[0]["_project_id"] == 1

    def test_skips_unresolvable_repo(self) -> None:
        with patch("lib.gitlab.resolve_project", return_value=None):
            assert gl.discover_mrs(["org/bad"], "alice") == []

    def test_skips_unresolvable_repo_verbose(self) -> None:
        with patch("lib.gitlab.resolve_project", return_value=None):
            assert gl.discover_mrs(["org/bad"], "alice", verbose=True) == []

    def test_verbose_prints_count(self) -> None:
        proj = ProjectInfo(project_id=1, path_with_namespace="org/repo", short_name="repo")
        with (
            patch("lib.gitlab.resolve_project", return_value=proj),
            patch("lib.gitlab.list_open_mrs", return_value=[]),
        ):
            gl.discover_mrs(["org/repo"], "alice", verbose=True)
