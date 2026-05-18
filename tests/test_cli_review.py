import builtins
import sys
import types
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

import teatree.backends.gitlab_api as gitlab_api_mod
import teatree.utils.run as utils_run_mod
from teatree.cli import app
from teatree.cli import review as review_mod
from teatree.cli.review import ReviewService, _find_added_line
from tests.teatree_core._on_behalf_gate_helpers import disable_on_behalf_gate

runner = CliRunner()


@pytest.fixture(autouse=True)
def _no_on_behalf_gate(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable the ``ask_before_post_on_behalf`` gate for these mechanics tests.

    The CLI's on-behalf gate (#960) is exercised by its own dedicated
    suite in ``tests/teatree_cli/test_review_on_behalf_gate.py``. These
    tests exercise GitLab API mechanics (post payload shape, line code
    validation, fallback to discussions) and need the gate OFF so the
    HTTP call actually happens and the mocked GitLabAPI sees the request.
    """
    disable_on_behalf_gate(tmp_path_factory, monkeypatch)


def _inline_api(changes_diff: str, post_result: dict[str, object] | None = None) -> MagicMock:
    """Build a mock GitLabAPI whose get_json returns MR data + a one-file changes diff."""
    api = MagicMock()
    mr_data = {"diff_refs": {"base_sha": "a", "head_sha": "b", "start_sha": "c"}}
    changes = {
        "changes": [
            {"new_path": "a.py", "old_path": "a.py", "diff": changes_diff},
        ],
    }
    api.get_json.side_effect = lambda endpoint: changes if "/changes" in endpoint else mr_data
    api.post_json.return_value = post_result or {"id": 99, "line_code": "h_0_1"}
    api.delete.return_value = 204
    return api


# -- _find_added_line --------------------------------------------------------


class TestFindAddedLine:
    def test_added_line_recognised(self):
        diff = "@@ -1,1 +1,2 @@\n unchanged\n+new line\n"
        is_added, nearby = _find_added_line(diff, 2)
        assert is_added
        assert nearby == [2]

    def test_context_line_rejected(self):
        diff = "@@ -1,2 +1,2 @@\n line one\n line two\n+added\n"
        is_added, _ = _find_added_line(diff, 1)
        assert not is_added

    def test_deleted_line_not_in_new_file(self):
        diff = "@@ -1,2 +1,1 @@\n keep\n-removed\n"
        is_added, _ = _find_added_line(diff, 2)
        assert not is_added

    def test_nearby_added_lines_collected(self):
        diff = "@@ -1,0 +1,5 @@\n+l1\n+l2\n+l3\n+l4\n+l5\n"
        _, nearby = _find_added_line(diff, 3)
        assert nearby == [1, 2, 3, 4, 5]

    def test_multi_hunk_offset_tracking(self):
        diff = "@@ -1,0 +1,1 @@\n+first\n@@ -10,0 +20,1 @@\n+second\n"
        is_added_first, _ = _find_added_line(diff, 1)
        is_added_second, _ = _find_added_line(diff, 20)
        is_added_gap, _ = _find_added_line(diff, 5)
        assert is_added_first
        assert is_added_second
        assert not is_added_gap


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

    def test_post_inline_added_line_succeeds(self, monkeypatch):
        """Inline draft note succeeds when target is an added line and GitLab returns a line_code."""
        monkeypatch.setenv("GITLAB_TOKEN", "test-token")
        diff = "@@ -0,0 +5,1 @@\n+added content\n"
        mock_api = _inline_api(diff, post_result={"id": 99, "line_code": "abc_0_5"})
        with patch.object(gitlab_api_mod, "GitLabAPI", return_value=mock_api):
            result = runner.invoke(
                app,
                ["review", "post-draft-note", "org/repo", "1", "fix this", "--file", "a.py", "--line", "5"],
            )
            assert result.exit_code == 0
            assert "OK draft_note_id=99" in result.output
            assert "line_code=abc_0_5" in result.output

    def test_post_inline_context_line_rejected_upfront(self, monkeypatch):
        """Targeting a context (unchanged) line is rejected before posting."""
        monkeypatch.setenv("GITLAB_TOKEN", "test-token")
        diff = "@@ -1,2 +1,2 @@\n keep one\n keep two\n+added\n"
        mock_api = _inline_api(diff)
        with patch.object(gitlab_api_mod, "GitLabAPI", return_value=mock_api):
            result = runner.invoke(
                app,
                ["review", "post-draft-note", "org/repo", "1", "msg", "--file", "a.py", "--line", "1"],
            )
            assert result.exit_code == 1
            assert "not an added" in result.output
            mock_api.post_json.assert_not_called()

    def test_post_inline_file_not_in_diff(self, monkeypatch):
        """Targeting a file not in the MR diff is rejected before posting."""
        monkeypatch.setenv("GITLAB_TOKEN", "test-token")
        mock_api = MagicMock()
        mock_api.get_json.side_effect = lambda endpoint: (
            {"changes": [{"new_path": "other.py", "old_path": "other.py", "diff": "@@ -0,0 +1,1 @@\n+x\n"}]}
            if "/changes" in endpoint
            else {"diff_refs": {"base_sha": "a", "head_sha": "b", "start_sha": "c"}}
        )
        with patch.object(gitlab_api_mod, "GitLabAPI", return_value=mock_api):
            result = runner.invoke(
                app,
                ["review", "post-draft-note", "org/repo", "1", "msg", "--file", "a.py", "--line", "1"],
            )
            assert result.exit_code == 1
            assert "not changed in MR" in result.output
            mock_api.post_json.assert_not_called()

    def test_post_inline_collapsed_diff_rejected_with_workaround(self, monkeypatch):
        """When the file diff is empty (collapsed), the draft is rejected with a workaround hint."""
        monkeypatch.setenv("GITLAB_TOKEN", "test-token")
        mock_api = MagicMock()
        mock_api.get_json.side_effect = lambda endpoint: (
            {"changes": [{"new_path": "a.py", "old_path": "a.py", "diff": ""}]}
            if "/changes" in endpoint
            else {"diff_refs": {"base_sha": "a", "head_sha": "b", "start_sha": "c"}}
        )
        with patch.object(gitlab_api_mod, "GitLabAPI", return_value=mock_api):
            result = runner.invoke(
                app,
                ["review", "post-draft-note", "org/repo", "1", "msg", "--file", "a.py", "--line", "5"],
            )
            assert result.exit_code == 1
            assert "no diff content" in result.output
            mock_api.post_json.assert_not_called()

    def test_post_inline_anchor_refused_deletes_broken_draft(self, monkeypatch):
        """When GitLab returns line_code=None, the broken draft is deleted and an error is surfaced."""
        monkeypatch.setenv("GITLAB_TOKEN", "test-token")
        diff = "@@ -0,0 +5,1 @@\n+added\n"
        mock_api = _inline_api(diff, post_result={"id": 42, "line_code": None})
        with patch.object(gitlab_api_mod, "GitLabAPI", return_value=mock_api):
            result = runner.invoke(
                app,
                ["review", "post-draft-note", "org/repo", "1", "msg", "--file", "a.py", "--line", "5"],
            )
            assert result.exit_code == 1
            assert "refused to anchor" in result.output
            assert "post-comment" in result.output
            mock_api.delete.assert_called_once()

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


# -- post-comment (immediate, non-draft) ---------------------------------------


class TestPostComment:
    def test_general_comment(self, monkeypatch):
        monkeypatch.setenv("GITLAB_TOKEN", "test-token")
        mock_api = MagicMock()
        mock_api.post_json.return_value = {"id": 555}
        with patch.object(gitlab_api_mod, "GitLabAPI", return_value=mock_api):
            result = runner.invoke(app, ["review", "post-comment", "org/repo", "1", "general body"])
            assert result.exit_code == 0
            assert "OK note_id=555" in result.output

    def test_inline_diff_note(self, monkeypatch):
        """post-comment anchors inline by posting via /discussions and verifying DiffNote."""
        monkeypatch.setenv("GITLAB_TOKEN", "test-token")
        diff = "@@ -0,0 +5,1 @@\n+added\n"
        mock_api = _inline_api(
            diff,
            post_result={"id": "disc-abc", "notes": [{"type": "DiffNote", "id": 1}]},
        )
        with patch.object(gitlab_api_mod, "GitLabAPI", return_value=mock_api):
            result = runner.invoke(
                app,
                ["review", "post-comment", "org/repo", "1", "msg", "--file", "a.py", "--line", "5"],
            )
            assert result.exit_code == 0
            assert "discussion_id=disc-abc" in result.output
            assert "inline DiffNote" in result.output

    def test_inline_anchor_falls_back_to_discussion_note(self, monkeypatch):
        """If GitLab posts a non-DiffNote (anchor lost), the command reports failure."""
        monkeypatch.setenv("GITLAB_TOKEN", "test-token")
        diff = "@@ -0,0 +5,1 @@\n+added\n"
        mock_api = _inline_api(
            diff,
            post_result={"id": "disc-xyz", "notes": [{"type": "DiscussionNote", "id": 2}]},
        )
        with patch.object(gitlab_api_mod, "GitLabAPI", return_value=mock_api):
            result = runner.invoke(
                app,
                ["review", "post-comment", "org/repo", "1", "msg", "--file", "a.py", "--line", "5"],
            )
            assert result.exit_code == 1
            assert "not anchored inline" in result.output

    def test_inline_context_line_rejected(self, monkeypatch):
        monkeypatch.setenv("GITLAB_TOKEN", "test-token")
        diff = "@@ -1,1 +1,1 @@\n unchanged\n"
        mock_api = _inline_api(diff)
        with patch.object(gitlab_api_mod, "GitLabAPI", return_value=mock_api):
            result = runner.invoke(
                app,
                ["review", "post-comment", "org/repo", "1", "msg", "--file", "a.py", "--line", "1"],
            )
            assert result.exit_code == 1
            assert "not an added" in result.output
            mock_api.post_json.assert_not_called()

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
        mock_api = MagicMock()
        mock_api.delete.return_value = 204
        with patch.object(gitlab_api_mod, "GitLabAPI", return_value=mock_api):
            result = runner.invoke(app, ["review", "delete-draft-note", "org/repo", "1", "42"])
            assert result.exit_code == 0
            assert "OK deleted" in result.output

    def test_delete_failure(self, monkeypatch):
        monkeypatch.setenv("GITLAB_TOKEN", "test-token")
        mock_api = MagicMock()
        mock_api.delete.return_value = 404
        with patch.object(gitlab_api_mod, "GitLabAPI", return_value=mock_api):
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
        mock_api = MagicMock()
        mock_api.post_status.return_value = 204
        with patch.object(gitlab_api_mod, "GitLabAPI", return_value=mock_api):
            result = runner.invoke(app, ["review", "publish-draft-notes", "org/repo", "1"])
            assert result.exit_code == 0
            assert "OK" in result.output

    def test_publish_failure(self, monkeypatch):
        monkeypatch.setenv("GITLAB_TOKEN", "test-token")
        mock_api = MagicMock()
        mock_api.post_status.return_value = 403
        with patch.object(gitlab_api_mod, "GitLabAPI", return_value=mock_api):
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
        mock_api = MagicMock()
        mock_api.put_status.return_value = 200
        with patch.object(gitlab_api_mod, "GitLabAPI", return_value=mock_api):
            result = runner.invoke(app, ["review", "resolve-discussion", "org/repo", "1", "abc123"])
            assert result.exit_code == 0
            assert "OK resolved=True" in result.output
            endpoint = mock_api.put_status.call_args.args[0]
            assert "discussions/abc123" in endpoint
            assert "resolved=true" in endpoint

    def test_unresolve_discussion_success(self, monkeypatch):
        monkeypatch.setenv("GITLAB_TOKEN", "test-token")
        mock_api = MagicMock()
        mock_api.put_status.return_value = 200
        with patch.object(gitlab_api_mod, "GitLabAPI", return_value=mock_api):
            result = runner.invoke(app, ["review", "resolve-discussion", "org/repo", "1", "abc123", "--no-resolved"])
            assert result.exit_code == 0
            assert "OK resolved=False" in result.output
            endpoint = mock_api.put_status.call_args.args[0]
            assert "resolved=false" in endpoint

    def test_resolve_discussion_failure(self, monkeypatch):
        monkeypatch.setenv("GITLAB_TOKEN", "test-token")
        mock_api = MagicMock()
        mock_api.put_status.return_value = 403
        with patch.object(gitlab_api_mod, "GitLabAPI", return_value=mock_api):
            result = runner.invoke(app, ["review", "resolve-discussion", "org/repo", "1", "abc123"])
            assert result.exit_code == 1
            assert "Failed: HTTP 403" in result.output

    def test_update_note_draft_success(self, monkeypatch):
        monkeypatch.setenv("GITLAB_TOKEN", "test-token")
        mock_api = MagicMock()
        mock_api.put_status.return_value = 200
        with patch.object(gitlab_api_mod, "GitLabAPI", return_value=mock_api):
            result = runner.invoke(app, ["review", "update-note", "org/repo", "1", "42", "new body"])
            assert result.exit_code == 0
            assert "OK updated draft_note_id=42" in result.output
            endpoint = mock_api.put_status.call_args.args[0]
            assert "draft_notes/42" in endpoint

    def test_update_note_falls_back_to_published(self, monkeypatch):
        monkeypatch.setenv("GITLAB_TOKEN", "test-token")
        mock_api = MagicMock()
        mock_api.put_status.side_effect = [404, 200]
        with patch.object(gitlab_api_mod, "GitLabAPI", return_value=mock_api):
            result = runner.invoke(app, ["review", "update-note", "org/repo", "1", "42", "new body"])
            assert result.exit_code == 0
            assert "OK updated note_id=42" in result.output
            assert mock_api.put_status.call_count == 2

    def test_update_note_published_failure(self, monkeypatch):
        monkeypatch.setenv("GITLAB_TOKEN", "test-token")
        mock_api = MagicMock()
        mock_api.put_status.side_effect = [404, 403]
        with patch.object(gitlab_api_mod, "GitLabAPI", return_value=mock_api):
            result = runner.invoke(app, ["review", "update-note", "org/repo", "1", "42", "new body"])
            assert result.exit_code == 1
            assert "Failed: HTTP 403" in result.output

    def test_update_note_draft_unexpected_status(self, monkeypatch):
        monkeypatch.setenv("GITLAB_TOKEN", "test-token")
        mock_api = MagicMock()
        mock_api.put_status.return_value = 403
        with patch.object(gitlab_api_mod, "GitLabAPI", return_value=mock_api):
            result = runner.invoke(app, ["review", "update-note", "org/repo", "1", "42", "new body"])
            assert result.exit_code == 1
            assert "Failed (draft): HTTP 403" in result.output
            assert mock_api.put_status.call_count == 1


# -- approve / unapprove -------------------------------------------------------


def _discussions_with_author(username: str) -> list[dict[str, object]]:
    """Build a discussions payload containing a note authored by ``username``."""
    return [
        {
            "id": "disc-1",
            "notes": [
                {"id": 1, "author": {"username": username}, "body": "reviewed: looks good"},
            ],
        },
    ]


class TestApprove:
    def test_approve_succeeds_when_reviewer_already_commented(self, monkeypatch):
        """Approve posts when a note authored by the approving identity exists."""
        monkeypatch.setenv("GITLAB_TOKEN", "test-token")
        mock_api = MagicMock()
        mock_api.current_username.return_value = "reviewer-bot"
        mock_api.get_json.side_effect = lambda endpoint: (
            _discussions_with_author("reviewer-bot") if "/discussions" in endpoint else {"id": 1}
        )
        mock_api.post_status.return_value = 201
        with patch.object(gitlab_api_mod, "GitLabAPI", return_value=mock_api):
            result = runner.invoke(app, ["review", "approve", "org/repo", "7"])
            assert result.exit_code == 0, result.output
            assert "OK approved" in result.output
            endpoint = mock_api.post_status.call_args.args[0]
            assert "merge_requests/7/approve" in endpoint

    def test_approve_refused_without_prior_review_note(self, monkeypatch):
        """Approve refuses (review-first precondition) when the identity has no note."""
        monkeypatch.setenv("GITLAB_TOKEN", "test-token")
        mock_api = MagicMock()
        mock_api.current_username.return_value = "reviewer-bot"
        mock_api.get_json.side_effect = lambda endpoint: (
            _discussions_with_author("someone-else") if "/discussions" in endpoint else {"id": 1}
        )
        with patch.object(gitlab_api_mod, "GitLabAPI", return_value=mock_api):
            result = runner.invoke(app, ["review", "approve", "org/repo", "7"])
            assert result.exit_code == 1
            assert "review before approve" in result.output
            mock_api.post_status.assert_not_called()

    def test_approve_refused_when_no_discussions(self, monkeypatch):
        """Approve refuses when the MR has no discussions at all."""
        monkeypatch.setenv("GITLAB_TOKEN", "test-token")
        mock_api = MagicMock()
        mock_api.current_username.return_value = "reviewer-bot"
        mock_api.get_json.side_effect = lambda endpoint: [] if "/discussions" in endpoint else {"id": 1}
        with patch.object(gitlab_api_mod, "GitLabAPI", return_value=mock_api):
            result = runner.invoke(app, ["review", "approve", "org/repo", "7"])
            assert result.exit_code == 1
            assert "review before approve" in result.output
            mock_api.post_status.assert_not_called()

    def test_approve_refused_when_discussions_not_a_list(self, monkeypatch):
        """A non-list discussions payload is treated as 'no review yet'."""
        monkeypatch.setenv("GITLAB_TOKEN", "test-token")
        mock_api = MagicMock()
        mock_api.current_username.return_value = "reviewer-bot"
        mock_api.get_json.side_effect = lambda endpoint: "unexpected" if "/discussions" in endpoint else {"id": 1}
        with patch.object(gitlab_api_mod, "GitLabAPI", return_value=mock_api):
            result = runner.invoke(app, ["review", "approve", "org/repo", "7"])
            assert result.exit_code == 1
            assert "review before approve" in result.output
            mock_api.post_status.assert_not_called()

    def test_approve_skips_malformed_discussion_and_note_shapes(self, monkeypatch):
        """Non-dict discussions, non-list notes, and non-dict notes are skipped."""
        monkeypatch.setenv("GITLAB_TOKEN", "test-token")
        mock_api = MagicMock()
        mock_api.current_username.return_value = "reviewer-bot"
        discussions = [
            "not-a-dict",
            {"id": "d2", "notes": "not-a-list"},
            {"id": "d3", "notes": ["not-a-dict-note"]},
            {"id": "d4", "notes": [{"id": 9, "author": {"username": "reviewer-bot"}}]},
        ]
        mock_api.get_json.side_effect = lambda endpoint: discussions if "/discussions" in endpoint else {"id": 1}
        mock_api.post_status.return_value = 201
        with patch.object(gitlab_api_mod, "GitLabAPI", return_value=mock_api):
            result = runner.invoke(app, ["review", "approve", "org/repo", "7"])
            assert result.exit_code == 0, result.output
            assert "OK approved" in result.output

    def test_approve_refused_when_identity_unknown(self, monkeypatch):
        """Approve refuses when the approving identity cannot be resolved."""
        monkeypatch.setenv("GITLAB_TOKEN", "test-token")
        mock_api = MagicMock()
        mock_api.current_username.return_value = ""
        mock_api.get_json.side_effect = lambda endpoint: (
            _discussions_with_author("reviewer-bot") if "/discussions" in endpoint else {"id": 1}
        )
        with patch.object(gitlab_api_mod, "GitLabAPI", return_value=mock_api):
            result = runner.invoke(app, ["review", "approve", "org/repo", "7"])
            assert result.exit_code == 1
            assert "Could not resolve" in result.output
            mock_api.post_status.assert_not_called()

    def test_approve_api_failure_surfaced(self, monkeypatch):
        """A non-2xx from the approve endpoint is surfaced as an error."""
        monkeypatch.setenv("GITLAB_TOKEN", "test-token")
        mock_api = MagicMock()
        mock_api.current_username.return_value = "reviewer-bot"
        mock_api.get_json.side_effect = lambda endpoint: (
            _discussions_with_author("reviewer-bot") if "/discussions" in endpoint else {"id": 1}
        )
        mock_api.post_status.return_value = 403
        with patch.object(gitlab_api_mod, "GitLabAPI", return_value=mock_api):
            result = runner.invoke(app, ["review", "approve", "org/repo", "7"])
            assert result.exit_code == 1
            assert "Failed: HTTP 403" in result.output

    def test_approve_blocked_by_on_behalf_gate(self, monkeypatch):
        """When the ask-before-post-on-behalf gate is active, approve refuses unattended."""
        monkeypatch.setenv("GITLAB_TOKEN", "test-token")
        mock_api = MagicMock()
        mock_api.current_username.return_value = "reviewer-bot"
        mock_api.get_json.side_effect = lambda endpoint: (
            _discussions_with_author("reviewer-bot") if "/discussions" in endpoint else {"id": 1}
        )
        with (
            patch.object(gitlab_api_mod, "GitLabAPI", return_value=mock_api),
            patch("teatree.cli.review._on_behalf_gate_active", return_value=True),
        ):
            result = runner.invoke(app, ["review", "approve", "org/repo", "7"])
            assert result.exit_code == 1
            assert "ask_before_post_on_behalf" in result.output
            mock_api.post_status.assert_not_called()

    def test_unapprove_succeeds(self, monkeypatch):
        """Unapprove posts to the unapprove endpoint with no review precondition."""
        monkeypatch.setenv("GITLAB_TOKEN", "test-token")
        mock_api = MagicMock()
        mock_api.post_status.return_value = 201
        with patch.object(gitlab_api_mod, "GitLabAPI", return_value=mock_api):
            result = runner.invoke(app, ["review", "unapprove", "org/repo", "7"])
            assert result.exit_code == 0, result.output
            assert "OK unapproved" in result.output
            endpoint = mock_api.post_status.call_args.args[0]
            assert "merge_requests/7/unapprove" in endpoint
            mock_api.current_username.assert_not_called()

    def test_unapprove_api_failure_surfaced(self, monkeypatch):
        monkeypatch.setenv("GITLAB_TOKEN", "test-token")
        mock_api = MagicMock()
        mock_api.post_status.return_value = 404
        with patch.object(gitlab_api_mod, "GitLabAPI", return_value=mock_api):
            result = runner.invoke(app, ["review", "unapprove", "org/repo", "7"])
            assert result.exit_code == 1
            assert "Failed: HTTP 404" in result.output

    def test_unapprove_blocked_by_on_behalf_gate(self, monkeypatch):
        monkeypatch.setenv("GITLAB_TOKEN", "test-token")
        mock_api = MagicMock()
        with (
            patch.object(gitlab_api_mod, "GitLabAPI", return_value=mock_api),
            patch("teatree.cli.review._on_behalf_gate_active", return_value=True),
        ):
            result = runner.invoke(app, ["review", "unapprove", "org/repo", "7"])
            assert result.exit_code == 1
            assert "ask_before_post_on_behalf" in result.output
            mock_api.post_status.assert_not_called()


class TestOnBehalfGateActive:
    def test_inactive_when_gate_module_absent(self, monkeypatch):
        """Absent gate module (not yet merged) -> integration point reports inactive."""
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "teatree.on_behalf_gate":
                raise ModuleNotFoundError(name)
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        assert review_mod._on_behalf_gate_active() is False

    def test_active_when_gate_module_present_and_enabled(self, monkeypatch):
        """Present gate module + enabled setting -> integration point reports active."""
        fake = types.ModuleType("teatree.on_behalf_gate")
        fake.ask_before_post_on_behalf_enabled = lambda: True
        monkeypatch.setitem(sys.modules, "teatree.on_behalf_gate", fake)
        assert review_mod._on_behalf_gate_active() is True

    def test_inactive_when_gate_module_present_but_disabled(self, monkeypatch):
        fake = types.ModuleType("teatree.on_behalf_gate")
        fake.ask_before_post_on_behalf_enabled = lambda: False
        monkeypatch.setitem(sys.modules, "teatree.on_behalf_gate", fake)
        assert review_mod._on_behalf_gate_active() is False


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

    def test_update_note_rejected(self, monkeypatch):
        monkeypatch.delenv("GITLAB_TOKEN", raising=False)
        with patch.object(utils_run_mod.subprocess, "run") as mock_run:
            mock_run.return_value = MagicMock(stderr="", returncode=1)
            result = runner.invoke(app, ["review", "update-note", "org/repo", "1", "42", "body"])
            assert result.exit_code == 1
            assert "No GitLab token" in result.output

    def test_post_comment_rejected(self, monkeypatch):
        monkeypatch.delenv("GITLAB_TOKEN", raising=False)
        with patch.object(utils_run_mod.subprocess, "run") as mock_run:
            mock_run.return_value = MagicMock(stderr="", returncode=1)
            result = runner.invoke(app, ["review", "post-comment", "org/repo", "1", "body"])
            assert result.exit_code == 1
            assert "No GitLab token" in result.output

    def test_approve_rejected(self, monkeypatch):
        monkeypatch.delenv("GITLAB_TOKEN", raising=False)
        with patch.object(utils_run_mod.subprocess, "run") as mock_run:
            mock_run.return_value = MagicMock(stderr="", returncode=1)
            result = runner.invoke(app, ["review", "approve", "org/repo", "7"])
            assert result.exit_code == 1
            assert "No GitLab token" in result.output

    def test_unapprove_rejected(self, monkeypatch):
        monkeypatch.delenv("GITLAB_TOKEN", raising=False)
        with patch.object(utils_run_mod.subprocess, "run") as mock_run:
            mock_run.return_value = MagicMock(stderr="", returncode=1)
            result = runner.invoke(app, ["review", "unapprove", "org/repo", "7"])
            assert result.exit_code == 1
            assert "No GitLab token" in result.output
