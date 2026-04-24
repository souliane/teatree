from unittest.mock import MagicMock, patch

import httpx
from typer.testing import CliRunner

import teatree.backends.gitlab_api as gitlab_api_mod
import teatree.utils.run as utils_run_mod
from teatree.cli import app
from teatree.cli.review import ReviewService

runner = CliRunner()


# -- GitLab token resolution --------------------------------------------------


class TestGetGitlabToken:
    def test_from_env(self, monkeypatch):
        monkeypatch.setenv("GITLAB_TOKEN", "gl-token-123")
        assert ReviewService.get_gitlab_token() == "gl-token-123"

    def test_from_glab(self, monkeypatch):
        monkeypatch.delenv("GITLAB_TOKEN", raising=False)
        with patch.object(utils_run_mod.subprocess, "run") as mock_run:
            mock_run.return_value = MagicMock(
                stderr="  Token: glpat-ABCDEF\n  User: test\n",
                returncode=0,
            )
            assert ReviewService.get_gitlab_token() == "glpat-ABCDEF"

    def test_returns_empty_when_not_found(self, monkeypatch):
        monkeypatch.delenv("GITLAB_TOKEN", raising=False)
        with patch.object(utils_run_mod.subprocess, "run") as mock_run:
            mock_run.return_value = MagicMock(stderr="", returncode=1)
            assert ReviewService.get_gitlab_token() == ""

    def test_returns_empty_when_glab_no_token_line(self, monkeypatch):
        """_get_gitlab_token returns empty when glab output has no Token line."""
        monkeypatch.delenv("GITLAB_TOKEN", raising=False)
        with patch.object(utils_run_mod.subprocess, "run") as mock_run:
            mock_run.return_value = MagicMock(
                stderr="  User: test\n  Scopes: api\n",
                returncode=0,
            )
            assert ReviewService.get_gitlab_token() == ""


# -- Review service operations -------------------------------------------------


class TestReviewService:
    def test_post_general(self, monkeypatch):
        """post-draft-note posts a general note (no file/line)."""
        monkeypatch.setenv("GITLAB_TOKEN", "test-token")
        mock_api = MagicMock()
        mock_api.post_json.return_value = {"id": 42, "position": None}

        with patch.object(gitlab_api_mod, "GitLabAPI", return_value=mock_api):
            result = runner.invoke(app, ["review", "post-draft-note", "org/repo", "1", "looks good"])
            assert result.exit_code == 0
            assert "OK draft_note_id=42" in result.output

    def test_post_inline(self, monkeypatch):
        """post-draft-note posts an inline note with file and line."""
        monkeypatch.setenv("GITLAB_TOKEN", "test-token")
        mock_api = MagicMock()
        mock_api.get_json.return_value = {
            "diff_refs": {
                "base_sha": "abc",
                "head_sha": "def",
                "start_sha": "ghi",
            },
        }
        mock_api.post_json.return_value = {
            "id": 99,
            "position": {"line_code": "abc_1_1"},
        }

        with patch.object(gitlab_api_mod, "GitLabAPI", return_value=mock_api):
            result = runner.invoke(
                app,
                ["review", "post-draft-note", "org/repo", "1", "fix this", "--file", "src/main.py", "--line", "10"],
            )
            assert result.exit_code == 0
            assert "OK draft_note_id=99" in result.output
            assert "line_code=abc_1_1" in result.output

    def test_post_inline_position_accepted_without_line_code(self, monkeypatch):
        """post-draft-note succeeds without warning when position has new_path."""
        monkeypatch.setenv("GITLAB_TOKEN", "test-token")
        mock_api = MagicMock()
        mock_api.get_json.return_value = {
            "diff_refs": {"base_sha": "a", "head_sha": "b", "start_sha": "c"},
        }
        mock_api.post_json.return_value = {"id": 100, "position": {"new_path": "a.py", "new_line": "5"}}

        with patch.object(gitlab_api_mod, "GitLabAPI", return_value=mock_api):
            result = runner.invoke(
                app,
                ["review", "post-draft-note", "org/repo", "1", "fix this", "--file", "a.py", "--line", "5"],
            )
            assert result.exit_code == 0
            assert "OK draft_note_id=100" in result.output
            assert "WARNING" not in result.output

    def test_post_inline_position_not_accepted(self, monkeypatch):
        """post-draft-note warns when GitLab returns empty position (inline placement rejected)."""
        monkeypatch.setenv("GITLAB_TOKEN", "test-token")
        mock_api = MagicMock()
        mock_api.get_json.return_value = {
            "diff_refs": {"base_sha": "a", "head_sha": "b", "start_sha": "c"},
        }
        mock_api.post_json.return_value = {"id": 100, "position": {}}

        with patch.object(gitlab_api_mod, "GitLabAPI", return_value=mock_api):
            result = runner.invoke(
                app,
                ["review", "post-draft-note", "org/repo", "1", "fix this", "--file", "a.py", "--line", "5"],
            )
            assert result.exit_code == 0
            assert "WARNING: inline position was not accepted" in result.output

    def test_post_mr_fetch_fails(self, monkeypatch):
        """post-draft-note fails when MR data cannot be fetched."""
        monkeypatch.setenv("GITLAB_TOKEN", "test-token")
        mock_api = MagicMock()
        mock_api.get_json.return_value = None

        with patch.object(gitlab_api_mod, "GitLabAPI", return_value=mock_api):
            result = runner.invoke(
                app,
                ["review", "post-draft-note", "org/repo", "1", "note", "--file", "a.py", "--line", "1"],
            )
            assert result.exit_code == 1
            assert "Could not fetch MR" in result.output

    def test_post_no_diff_refs(self, monkeypatch):
        """post-draft-note fails when MR has no diff_refs."""
        monkeypatch.setenv("GITLAB_TOKEN", "test-token")
        mock_api = MagicMock()
        mock_api.get_json.return_value = {"diff_refs": None}

        with patch.object(gitlab_api_mod, "GitLabAPI", return_value=mock_api):
            result = runner.invoke(
                app,
                ["review", "post-draft-note", "org/repo", "1", "note", "--file", "a.py", "--line", "1"],
            )
            assert result.exit_code == 1
            assert "no diff_refs" in result.output

    def test_post_fails(self, monkeypatch):
        """post-draft-note fails when the POST returns empty."""
        monkeypatch.setenv("GITLAB_TOKEN", "test-token")
        mock_api = MagicMock()
        mock_api.post_json.return_value = None

        with patch.object(gitlab_api_mod, "GitLabAPI", return_value=mock_api):
            result = runner.invoke(app, ["review", "post-draft-note", "org/repo", "1", "note"])
            assert result.exit_code == 1
            assert "Failed to post" in result.output

    def test_delete_success(self, monkeypatch):
        monkeypatch.setenv("GITLAB_TOKEN", "test-token")
        mock_response = MagicMock(status_code=204)
        with patch.object(httpx, "delete", return_value=mock_response):
            result = runner.invoke(app, ["review", "delete-draft-note", "org/repo", "1", "42"])
            assert result.exit_code == 0
            assert "OK deleted" in result.output

    def test_delete_failure(self, monkeypatch):
        monkeypatch.setenv("GITLAB_TOKEN", "test-token")
        mock_response = MagicMock(status_code=404)
        with patch.object(httpx, "delete", return_value=mock_response):
            result = runner.invoke(app, ["review", "delete-draft-note", "org/repo", "1", "42"])
            assert result.exit_code == 1
            assert "Failed: HTTP 404" in result.output

    def test_list_success(self, monkeypatch):
        monkeypatch.setenv("GITLAB_TOKEN", "test-token")
        mock_api = MagicMock()
        mock_api.get_json.return_value = [
            {"id": 1, "note": "first note text", "position": {"new_path": "a.py", "new_line": 10}},
            {"id": 2, "note": "second note", "position": None},
            "not a dict",
        ]
        with patch.object(gitlab_api_mod, "GitLabAPI", return_value=mock_api):
            result = runner.invoke(app, ["review", "list-draft-notes", "org/repo", "1"])
            assert result.exit_code == 0
            assert "a.py:10" in result.output

    def test_list_none_found(self, monkeypatch):
        monkeypatch.setenv("GITLAB_TOKEN", "test-token")
        mock_api = MagicMock()
        mock_api.get_json.return_value = "not a list"
        with patch.object(gitlab_api_mod, "GitLabAPI", return_value=mock_api):
            result = runner.invoke(app, ["review", "list-draft-notes", "org/repo", "1"])
            assert result.exit_code == 0
            assert "No draft notes" in result.output

    def test_publish_success(self, monkeypatch):
        monkeypatch.setenv("GITLAB_TOKEN", "test-token")
        mock_response = MagicMock(status_code=204)
        with patch.object(httpx, "post", return_value=mock_response):
            result = runner.invoke(app, ["review", "publish-draft-notes", "org/repo", "1"])
            assert result.exit_code == 0
            assert "OK" in result.output

    def test_publish_failure(self, monkeypatch):
        monkeypatch.setenv("GITLAB_TOKEN", "test-token")
        mock_response = MagicMock(status_code=403)
        with patch.object(httpx, "post", return_value=mock_response):
            result = runner.invoke(app, ["review", "publish-draft-notes", "org/repo", "1"])
            assert result.exit_code == 1
            assert "Failed: HTTP 403" in result.output

    def test_reply_to_discussion_success(self, monkeypatch):
        monkeypatch.setenv("GITLAB_TOKEN", "test-token")
        mock_api = MagicMock()
        mock_api.post_json.return_value = {"id": 777, "body": "thanks"}
        with patch.object(gitlab_api_mod, "GitLabAPI", return_value=mock_api):
            result = runner.invoke(
                app,
                ["review", "reply-to-discussion", "org/repo", "1", "abc123", "thanks"],
            )
            assert result.exit_code == 0
            assert "OK reply_note_id=777" in result.output
            mock_api.post_json.assert_called_once()
            endpoint, payload = mock_api.post_json.call_args.args
            assert "discussions/abc123/notes" in endpoint
            assert payload == {"body": "thanks"}

    def test_reply_to_discussion_failure(self, monkeypatch):
        monkeypatch.setenv("GITLAB_TOKEN", "test-token")
        mock_api = MagicMock()
        mock_api.post_json.return_value = None
        with patch.object(gitlab_api_mod, "GitLabAPI", return_value=mock_api):
            result = runner.invoke(
                app,
                ["review", "reply-to-discussion", "org/repo", "1", "abc123", "thanks"],
            )
            assert result.exit_code == 1
            assert "Failed to post reply" in result.output

    def test_resolve_discussion_success(self, monkeypatch):
        monkeypatch.setenv("GITLAB_TOKEN", "test-token")
        mock_response = MagicMock(status_code=200)
        with patch.object(httpx, "put", return_value=mock_response) as mock_put:
            result = runner.invoke(
                app,
                ["review", "resolve-discussion", "org/repo", "1", "abc123"],
            )
            assert result.exit_code == 0
            assert "OK resolved=True" in result.output
            call_url = mock_put.call_args.args[0]
            assert "discussions/abc123" in call_url
            assert "resolved=true" in call_url

    def test_unresolve_discussion_success(self, monkeypatch):
        monkeypatch.setenv("GITLAB_TOKEN", "test-token")
        mock_response = MagicMock(status_code=200)
        with patch.object(httpx, "put", return_value=mock_response) as mock_put:
            result = runner.invoke(
                app,
                ["review", "resolve-discussion", "org/repo", "1", "abc123", "--no-resolved"],
            )
            assert result.exit_code == 0
            assert "OK resolved=False" in result.output
            call_url = mock_put.call_args.args[0]
            assert "resolved=false" in call_url

    def test_resolve_discussion_failure(self, monkeypatch):
        monkeypatch.setenv("GITLAB_TOKEN", "test-token")
        mock_response = MagicMock(status_code=403)
        with patch.object(httpx, "put", return_value=mock_response):
            result = runner.invoke(
                app,
                ["review", "resolve-discussion", "org/repo", "1", "abc123"],
            )
            assert result.exit_code == 1
            assert "Failed: HTTP 403" in result.output


# -- _require_token helper -----------------------------------------------------


class TestRequireToken:
    def test_post_draft_note_rejected(self, monkeypatch):
        monkeypatch.delenv("GITLAB_TOKEN", raising=False)
        monkeypatch.delenv("GITLAB_TOKEN", raising=False)
        with patch.object(utils_run_mod.subprocess, "run") as mock_run:
            mock_run.return_value = MagicMock(stderr="", returncode=1)
            result = runner.invoke(app, ["review", "post-draft-note", "org/repo", "1", "note"])
            assert result.exit_code == 1
            assert "No GitLab token" in result.output

    def test_delete_draft_note_rejected(self, monkeypatch):
        monkeypatch.delenv("GITLAB_TOKEN", raising=False)
        monkeypatch.delenv("GITLAB_TOKEN", raising=False)
        with patch.object(utils_run_mod.subprocess, "run") as mock_run:
            mock_run.return_value = MagicMock(stderr="", returncode=1)
            result = runner.invoke(app, ["review", "delete-draft-note", "org/repo", "1", "42"])
            assert result.exit_code == 1
            assert "No GitLab token" in result.output

    def test_publish_draft_notes_rejected(self, monkeypatch):
        monkeypatch.delenv("GITLAB_TOKEN", raising=False)
        with patch.object(utils_run_mod.subprocess, "run") as mock_run:
            mock_run.return_value = MagicMock(stderr="", returncode=1)
            result = runner.invoke(app, ["review", "publish-draft-notes", "org/repo", "1"])
            assert result.exit_code == 1
            assert "No GitLab token" in result.output

    def test_list_draft_notes_rejected(self, monkeypatch):
        monkeypatch.delenv("GITLAB_TOKEN", raising=False)
        monkeypatch.delenv("GITLAB_TOKEN", raising=False)
        with patch.object(utils_run_mod.subprocess, "run") as mock_run:
            mock_run.return_value = MagicMock(stderr="", returncode=1)
            result = runner.invoke(app, ["review", "list-draft-notes", "org/repo", "1"])
            assert result.exit_code == 1
            assert "No GitLab token" in result.output

    def test_reply_to_discussion_rejected(self, monkeypatch):
        monkeypatch.delenv("GITLAB_TOKEN", raising=False)
        with patch.object(utils_run_mod.subprocess, "run") as mock_run:
            mock_run.return_value = MagicMock(stderr="", returncode=1)
            result = runner.invoke(app, ["review", "reply-to-discussion", "org/repo", "1", "abc", "hi"])
            assert result.exit_code == 1
            assert "No GitLab token" in result.output

    def test_resolve_discussion_rejected(self, monkeypatch):
        monkeypatch.delenv("GITLAB_TOKEN", raising=False)
        with patch.object(utils_run_mod.subprocess, "run") as mock_run:
            mock_run.return_value = MagicMock(stderr="", returncode=1)
            result = runner.invoke(app, ["review", "resolve-discussion", "org/repo", "1", "abc"])
            assert result.exit_code == 1
            assert "No GitLab token" in result.output
