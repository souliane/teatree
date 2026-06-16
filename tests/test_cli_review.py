from http import HTTPStatus
from unittest.mock import MagicMock, patch

import httpx
import pytest
from typer.testing import CliRunner

import teatree.backends.gitlab.api as gitlab_api_mod
import teatree.utils.run as utils_run_mod
from teatree.cli import app
from teatree.cli.review import ReviewService
from teatree.cli.review.service import _find_added_line
from tests.teatree_core._on_behalf_gate_helpers import disable_on_behalf_gate

runner = CliRunner()

# Every publishing method now fires the #949 after-receipt visibility DM
# (souliane/teatree#949), which resolves ``get_effective_settings()`` and
# records a ``BotPing`` row — both touch the ORM. The gate-mechanics
# tests below therefore need DB access (the after-receipt DM is an
# intrinsic side effect of the publish path, not test scaffolding).
# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _no_on_behalf_gate(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """Set ``on_behalf_post_mode = "immediate"`` for these mechanics tests.

    The CLI's on-behalf gate (#960) is exercised by its own dedicated
    suite in ``tests/teatree_cli/test_review_on_behalf_gate.py``. These
    tests exercise GitLab API mechanics (post payload shape, line code
    validation, fallback to discussions) and need the gate OFF so the
    HTTP call actually happens and the mocked GitLabAPI sees the request.
    """
    disable_on_behalf_gate(tmp_path_factory, monkeypatch)


def _readback_404(_endpoint: str) -> object:
    """A read-back side_effect that 404s — confirms a delete took (#2081)."""
    request = httpx.Request("GET", "https://gitlab.example/api/v4/x")
    response = httpx.Response(HTTPStatus.NOT_FOUND, request=request)
    msg = "not found"
    raise httpx.HTTPStatusError(msg, request=request, response=response)


def _confirming_readback(endpoint: str, *, approver: str = "reviewer-bot") -> object:
    """Verify-after-post (#2081) read-back side_effect: every artifact confirms it landed.

    A note/draft note by id → present; ``/approvals`` → ``approver`` approved;
    the bulk-publish lists → drafts flushed + an authored note present; a
    discussion → resolved notes. Tests that need the inverse (a deleted note,
    an unapprove) override this for the specific endpoint.
    """
    last = endpoint.rstrip("/").rsplit("/", 1)[-1]
    if last.isdigit():
        return {"id": int(last), "resolvable": True, "resolved": True}
    if endpoint.endswith("/approvals"):
        return {"approved_by": [{"user": {"username": approver}}]}
    if last == "draft_notes":
        return []
    if last == "notes":
        return [{"id": 99, "author": {"username": approver}}]
    if "discussions/" in endpoint:
        return {"notes": [{"resolvable": True, "resolved": True}]}
    return {}


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
        """post-draft-note posts a general note when ``--general`` is explicit."""
        monkeypatch.setenv("GITLAB_TOKEN", "test-token")
        mock_api = MagicMock()
        mock_api.post_json.return_value = {"id": 42, "position": None}

        with patch.object(gitlab_api_mod, "GitLabAPI", return_value=mock_api):
            result = runner.invoke(
                app,
                ["review", "post-draft-note", "org/repo", "1", "looks good", "--general"],
            )
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


# -- post-comment (DRAFT by default — #1207 — and --live opt-in) ----------------


class TestPostComment:
    def test_general_comment_defaults_to_draft(self, monkeypatch):
        """Default ``post-comment`` (no ``--live``) creates a draft (#1207)."""
        monkeypatch.setenv("GITLAB_TOKEN", "test-token")
        mock_api = MagicMock()
        mock_api.post_json.return_value = {"id": 555}
        with patch.object(gitlab_api_mod, "GitLabAPI", return_value=mock_api):
            result = runner.invoke(app, ["review", "post-comment", "org/repo", "1", "general body"])
            assert result.exit_code == 0
            assert "draft_note_id=555" in result.output

    def test_inline_diff_note_with_live_flag(self, monkeypatch):
        """``post-comment --live`` (with a recorded approval) posts the inline DiffNote."""
        from teatree.core.models import LivePostApproval  # noqa: PLC0415

        LivePostApproval.record(mr_url="org/repo!1", slack_ts="1700000000.0001", slack_user_id="U-OP")
        monkeypatch.setenv("GITLAB_TOKEN", "test-token")
        diff = "@@ -0,0 +5,1 @@\n+added\n"
        mock_api = _inline_api(
            diff,
            post_result={
                "id": "disc-abc",
                "notes": [{"type": "DiffNote", "id": 1, "position": {"new_path": "a.py", "new_line": 5}}],
            },
        )
        with patch.object(gitlab_api_mod, "GitLabAPI", return_value=mock_api):
            result = runner.invoke(
                app,
                ["review", "post-comment", "org/repo", "1", "msg", "--file", "a.py", "--line", "5", "--live"],
            )
            assert result.exit_code == 0
            assert "discussion_id=disc-abc" in result.output
            assert "inline DiffNote" in result.output

    def test_inline_live_post_refused_without_approval(self, monkeypatch):
        """``post-comment --live`` without a recorded approval refuses (#1207)."""
        monkeypatch.setenv("GITLAB_TOKEN", "test-token")
        diff = "@@ -0,0 +5,1 @@\n+added\n"
        mock_api = _inline_api(diff)
        with patch.object(gitlab_api_mod, "GitLabAPI", return_value=mock_api):
            result = runner.invoke(
                app,
                ["review", "post-comment", "org/repo", "1", "msg", "--file", "a.py", "--line", "5", "--live"],
            )
            assert result.exit_code == 1
            assert "approve-live-post" in result.output

    def test_inline_anchor_downgrade_hard_fails(self, monkeypatch):
        """If GitLab silently downgrades to an MR-level note (no position.new_path), rc=1 (#1161).

        The comment IS live on GitLab, but it is NOT anchored on the diff. The
        EXTREMELY RED CARD: a sub-agent must never report "inline POSTs
        succeeded" while every post was MR-level. The claim is still recorded so
        a retry is idempotent, but the non-zero exit code refuses the false
        success.
        """
        from teatree.core.models import LivePostApproval  # noqa: PLC0415

        LivePostApproval.record(mr_url="org/repo!1", slack_ts="1700000000.0001", slack_user_id="U-OP")
        monkeypatch.setenv("GITLAB_TOKEN", "test-token")
        diff = "@@ -0,0 +5,1 @@\n+added\n"
        mock_api = _inline_api(
            diff,
            post_result={"id": "disc-xyz", "notes": [{"type": "DiscussionNote", "id": 2, "position": None}]},
        )
        with patch.object(gitlab_api_mod, "GitLabAPI", return_value=mock_api):
            result = runner.invoke(
                app,
                ["review", "post-comment", "org/repo", "1", "msg", "--file", "a.py", "--line", "5", "--live"],
            )
            assert result.exit_code == 1
            assert "MR-level" in result.output

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
        """post-draft-note fails when the POST returns empty (general path)."""
        monkeypatch.setenv("GITLAB_TOKEN", "test-token")
        mock_api = MagicMock()
        mock_api.post_json.return_value = None

        with patch.object(gitlab_api_mod, "GitLabAPI", return_value=mock_api):
            result = runner.invoke(app, ["review", "post-draft-note", "org/repo", "1", "note", "--general"])
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
        mock_api.get_json.side_effect = _confirming_readback
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
        mock_api.get_json.side_effect = lambda ep: {"notes": [{"resolvable": True, "resolved": True}]}
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
        mock_api.get_json.side_effect = lambda ep: {"notes": [{"resolvable": True, "resolved": False}]}
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


# -- post-draft-note --general flag (silent-degradation guard) ----------------


class TestPostDraftNoteGeneralFlag:
    """Inline by default, general only with explicit ``--general`` (#72).

    The pre-#72 default silently degraded a missing ``--file``/``--line``
    pair to a general MR-wide note — observed in !6220 where 4 of 5
    cold-review drafts intended as inline became general. The fix makes
    the inline-vs-general decision explicit at the typer wrapper:

    * Without ``--general``: both ``--file`` and ``--line`` are required.
    * With ``--general``: both ``--file`` and ``--line`` must be absent.

    Validation lives in the wrapper, not the service body — the service
    contract stays ``post_draft_note(..., file: str = '', line: int = 0)``
    so the existing service-level tests stay green.
    """

    def test_inline_with_file_and_line_succeeds(self, monkeypatch):
        """Sanity: a normal inline draft still works."""
        monkeypatch.setenv("GITLAB_TOKEN", "test-token")
        diff = "@@ -0,0 +5,1 @@\n+added content\n"
        mock_api = _inline_api(diff, post_result={"id": 99, "line_code": "abc_0_5"})
        with patch.object(gitlab_api_mod, "GitLabAPI", return_value=mock_api):
            result = runner.invoke(
                app,
                ["review", "post-draft-note", "org/repo", "1", "msg", "--file", "a.py", "--line", "5"],
            )
            assert result.exit_code == 0, result.output

    def test_general_flag_with_no_file_or_line_succeeds(self, monkeypatch):
        """``--general`` posts the general (MR-wide) draft note."""
        monkeypatch.setenv("GITLAB_TOKEN", "test-token")
        mock_api = MagicMock()
        mock_api.post_json.return_value = {"id": 42}
        with patch.object(gitlab_api_mod, "GitLabAPI", return_value=mock_api):
            result = runner.invoke(app, ["review", "post-draft-note", "org/repo", "1", "general", "--general"])
            assert result.exit_code == 0, result.output
            assert "OK draft_note_id=42" in result.output

    def test_missing_file_and_line_refused_without_general(self, monkeypatch):
        """Omitting both ``--file`` and ``--line`` without ``--general`` is refused.

        Foot-gun the change closes: pre-#72 this silently posted as a
        general MR-wide note, degrading 4 of 5 intended-inline drafts on
        !6220. The HTTP call MUST NOT happen — refusal is upfront.
        """
        monkeypatch.setenv("GITLAB_TOKEN", "test-token")
        mock_api = MagicMock()
        with patch.object(gitlab_api_mod, "GitLabAPI", return_value=mock_api):
            result = runner.invoke(app, ["review", "post-draft-note", "org/repo", "1", "body only"])
            assert result.exit_code == 1
            assert "--file" in result.output
            assert "--line" in result.output
            assert "--general" in result.output
            mock_api.post_json.assert_not_called()

    def test_only_file_without_line_refused(self, monkeypatch):
        """``--file`` without ``--line`` is refused (incomplete inline target)."""
        monkeypatch.setenv("GITLAB_TOKEN", "test-token")
        mock_api = MagicMock()
        with patch.object(gitlab_api_mod, "GitLabAPI", return_value=mock_api):
            result = runner.invoke(
                app,
                ["review", "post-draft-note", "org/repo", "1", "body", "--file", "a.py"],
            )
            assert result.exit_code == 1
            assert "--line" in result.output
            mock_api.post_json.assert_not_called()

    def test_only_line_without_file_refused(self, monkeypatch):
        """``--line`` without ``--file`` is refused (incomplete inline target)."""
        monkeypatch.setenv("GITLAB_TOKEN", "test-token")
        mock_api = MagicMock()
        with patch.object(gitlab_api_mod, "GitLabAPI", return_value=mock_api):
            result = runner.invoke(
                app,
                ["review", "post-draft-note", "org/repo", "1", "body", "--line", "5"],
            )
            assert result.exit_code == 1
            assert "--file" in result.output
            mock_api.post_json.assert_not_called()

    def test_general_with_file_refused(self, monkeypatch):
        """``--general`` is mutually exclusive with ``--file``."""
        monkeypatch.setenv("GITLAB_TOKEN", "test-token")
        mock_api = MagicMock()
        with patch.object(gitlab_api_mod, "GitLabAPI", return_value=mock_api):
            result = runner.invoke(
                app,
                ["review", "post-draft-note", "org/repo", "1", "body", "--general", "--file", "a.py"],
            )
            assert result.exit_code == 1
            assert "mutually exclusive" in result.output or "--general" in result.output
            mock_api.post_json.assert_not_called()

    def test_general_with_line_refused(self, monkeypatch):
        """``--general`` is mutually exclusive with ``--line``."""
        monkeypatch.setenv("GITLAB_TOKEN", "test-token")
        mock_api = MagicMock()
        with patch.object(gitlab_api_mod, "GitLabAPI", return_value=mock_api):
            result = runner.invoke(
                app,
                ["review", "post-draft-note", "org/repo", "1", "body", "--general", "--line", "5"],
            )
            assert result.exit_code == 1
            assert "mutually exclusive" in result.output or "--general" in result.output
            mock_api.post_json.assert_not_called()

    def test_general_with_file_and_line_refused(self, monkeypatch):
        """``--general`` is mutually exclusive with both ``--file`` and ``--line`` together."""
        monkeypatch.setenv("GITLAB_TOKEN", "test-token")
        mock_api = MagicMock()
        with patch.object(gitlab_api_mod, "GitLabAPI", return_value=mock_api):
            result = runner.invoke(
                app,
                [
                    "review",
                    "post-draft-note",
                    "org/repo",
                    "1",
                    "body",
                    "--general",
                    "--file",
                    "a.py",
                    "--line",
                    "5",
                ],
            )
            assert result.exit_code == 1
            assert "mutually exclusive" in result.output or "--general" in result.output
            mock_api.post_json.assert_not_called()


# -- delete-discussion (published note removal) -------------------------------


class TestDeleteDiscussion:
    """``delete-discussion`` removes a published note via DELETE /notes/{id}."""

    def test_delete_discussion_success(self, monkeypatch):
        """A 204 NO_CONTENT response reports OK and exits 0."""
        monkeypatch.setenv("GITLAB_TOKEN", "test-token")
        mock_api = MagicMock()
        mock_api.delete.return_value = 204
        # Verify-after-delete (#2081): the read-back of the deleted note 404s,
        # confirming it is gone.
        mock_api.get_json.side_effect = _readback_404
        with patch.object(gitlab_api_mod, "GitLabAPI", return_value=mock_api):
            result = runner.invoke(app, ["review", "delete-discussion", "org/repo", "1", "99"])
            assert result.exit_code == 0
            assert "OK deleted note_id=99" in result.output
            endpoint = mock_api.delete.call_args.args[0]
            # GitLab endpoint shape: /projects/{encoded}/merge_requests/{mr}/notes/{note_id}
            assert "merge_requests/1/notes/99" in endpoint
            assert "draft_notes" not in endpoint

    def test_delete_discussion_failure_surfaces_http_status(self, monkeypatch):
        """A non-204 response is surfaced as ``Failed: HTTP <status>`` with that exit code."""
        monkeypatch.setenv("GITLAB_TOKEN", "test-token")
        mock_api = MagicMock()
        mock_api.delete.return_value = 404
        with patch.object(gitlab_api_mod, "GitLabAPI", return_value=mock_api):
            result = runner.invoke(app, ["review", "delete-discussion", "org/repo", "1", "99"])
            assert result.exit_code == 404
            assert "Failed: HTTP 404" in result.output

    def test_delete_discussion_token_rejected(self, monkeypatch):
        """No token → ``No GitLab token`` refusal, no HTTP call."""
        monkeypatch.delenv("GITLAB_TOKEN", raising=False)
        with patch.object(utils_run_mod.subprocess, "run") as mock_run:
            mock_run.return_value = MagicMock(stderr="", returncode=1)
            result = runner.invoke(app, ["review", "delete-discussion", "org/repo", "1", "99"])
            assert result.exit_code == 1
            assert "No GitLab token" in result.output


# -- delete-issue-note (published ISSUE/work-item note removal) ----------------


class TestDeleteIssueNote:
    """``delete-issue-note`` removes a published issue note via DELETE /issues/{iid}/notes/{id}."""

    def test_delete_issue_note_success_hits_issue_endpoint(self, monkeypatch):
        """A 204 reports OK and targets the ISSUE notes endpoint (never merge_requests)."""
        monkeypatch.setenv("GITLAB_TOKEN", "test-token")
        mock_api = MagicMock()
        mock_api.delete.return_value = 204
        mock_api.get_json.side_effect = _readback_404
        with patch.object(gitlab_api_mod, "GitLabAPI", return_value=mock_api):
            result = runner.invoke(app, ["review", "delete-issue-note", "org/repo", "8568", "3456141979"])
            assert result.exit_code == 0, result.output
            assert "OK deleted note_id=3456141979 on issue #8568" in result.output
            endpoint = mock_api.delete.call_args.args[0]
            assert "issues/8568/notes/3456141979" in endpoint
            assert "merge_requests" not in endpoint

    def test_delete_issue_note_failure_surfaces_http_status(self, monkeypatch):
        """A non-204 response is surfaced as ``Failed: HTTP <status>`` with that exit code."""
        monkeypatch.setenv("GITLAB_TOKEN", "test-token")
        mock_api = MagicMock()
        mock_api.delete.return_value = 404
        with patch.object(gitlab_api_mod, "GitLabAPI", return_value=mock_api):
            result = runner.invoke(app, ["review", "delete-issue-note", "org/repo", "8568", "99"])
            assert result.exit_code == 404
            assert "Failed: HTTP 404" in result.output

    def test_delete_issue_note_unverified_when_note_survives(self, monkeypatch):
        """ANTI-VACUITY: a 204 whose read-back still finds the note must NOT report deleted.

        verify_issue_note_deleted (#2081) reads the note back; a 200 means the
        delete did not take, so the call rolls back to a failure, never a
        phantom "OK deleted".
        """
        monkeypatch.setenv("GITLAB_TOKEN", "test-token")
        mock_api = MagicMock()
        mock_api.delete.return_value = 204
        # The deleted note is STILL present on read-back — the delete was a no-op.
        mock_api.get_json.return_value = {"id": 99}
        with patch.object(gitlab_api_mod, "GitLabAPI", return_value=mock_api):
            result = runner.invoke(app, ["review", "delete-issue-note", "org/repo", "8568", "99"])
            assert result.exit_code != 0
            assert "OK deleted" not in result.output
            assert "still present" in result.output

    def test_delete_issue_note_token_rejected(self, monkeypatch):
        """No token → ``No GitLab token`` refusal, no HTTP call."""
        monkeypatch.delenv("GITLAB_TOKEN", raising=False)
        with patch.object(utils_run_mod.subprocess, "run") as mock_run:
            mock_run.return_value = MagicMock(stderr="", returncode=1)
            result = runner.invoke(app, ["review", "delete-issue-note", "org/repo", "8568", "99"])
            assert result.exit_code == 1
            assert "No GitLab token" in result.output


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
        # Verify-after-approve (#2081): the /approvals read-back must show the
        # identity present so the confirmed approve stays green.
        mock_api.get_json.side_effect = lambda endpoint: (
            {"approved_by": [{"user": {"username": "reviewer-bot"}}]} if endpoint.endswith("/approvals") else {"id": 1}
        )
        mock_api.get_json_paginated.side_effect = lambda endpoint: (
            _discussions_with_author("reviewer-bot") if "/discussions" in endpoint else []
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
        mock_api.get_json.side_effect = lambda endpoint: {"id": 1}
        mock_api.get_json_paginated.side_effect = lambda endpoint: (
            _discussions_with_author("someone-else") if "/discussions" in endpoint else []
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
        mock_api.get_json.side_effect = lambda endpoint: {"id": 1}
        mock_api.get_json_paginated.side_effect = lambda endpoint: []
        with patch.object(gitlab_api_mod, "GitLabAPI", return_value=mock_api):
            result = runner.invoke(app, ["review", "approve", "org/repo", "7"])
            assert result.exit_code == 1
            assert "review before approve" in result.output
            mock_api.post_status.assert_not_called()

    def test_approve_refused_when_discussions_only_have_other_authors(self, monkeypatch):
        """Approve refuses when discussions exist but none authored by the approving identity."""
        monkeypatch.setenv("GITLAB_TOKEN", "test-token")
        mock_api = MagicMock()
        mock_api.current_username.return_value = "reviewer-bot"
        mock_api.get_json.side_effect = lambda endpoint: {"id": 1}
        mock_api.get_json_paginated.side_effect = lambda endpoint: (
            _discussions_with_author("unrelated-user") if "/discussions" in endpoint else []
        )
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
        mock_api.get_json.side_effect = lambda endpoint: (
            {"approved_by": [{"user": {"username": "reviewer-bot"}}]} if endpoint.endswith("/approvals") else {"id": 1}
        )
        mock_api.get_json_paginated.side_effect = lambda endpoint: discussions if "/discussions" in endpoint else []
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
        mock_api.get_json.side_effect = lambda endpoint: {"id": 1}
        mock_api.get_json_paginated.side_effect = lambda endpoint: (
            _discussions_with_author("reviewer-bot") if "/discussions" in endpoint else []
        )
        mock_api.post_status.return_value = 403
        with patch.object(gitlab_api_mod, "GitLabAPI", return_value=mock_api):
            result = runner.invoke(app, ["review", "approve", "org/repo", "7"])
            assert result.exit_code == 1
            assert "Failed: HTTP 403" in result.output

    # ast-grep-ignore: ac-django-no-pytest-django-db
    @pytest.mark.django_db
    def test_approve_blocked_by_on_behalf_gate(self, tmp_path, monkeypatch):
        """Gate ON + no recorded approval → approve refuses without an API call (#1013)."""
        monkeypatch.setenv("GITLAB_TOKEN", "test-token")
        # The autouse ``_no_on_behalf_gate`` fixture sets the gate to immediate
        # via ``T3_ON_BEHALF_POST_MODE`` (``on_behalf_post_mode`` is DB-home,
        # #1775). This test needs the gate ON, so undo that override and let the
        # mode resolve to its blocking ``DRAFT_OR_ASK`` default. (The old
        # ``ask_before_post_on_behalf`` TOML staging is inert — that field is
        # derived from ``on_behalf_post_mode`` and ignored on read.)
        monkeypatch.delenv("T3_ON_BEHALF_POST_MODE", raising=False)
        mock_api = MagicMock()
        mock_api.current_username.return_value = "reviewer-bot"
        mock_api.get_json.side_effect = lambda endpoint: {"id": 1}
        mock_api.get_json_paginated.side_effect = lambda endpoint: (
            _discussions_with_author("reviewer-bot") if "/discussions" in endpoint else []
        )
        with patch.object(gitlab_api_mod, "GitLabAPI", return_value=mock_api):
            result = runner.invoke(app, ["review", "approve", "org/repo", "7"])
            assert result.exit_code == 1
            assert "approve-on-behalf" in result.output
            mock_api.post_status.assert_not_called()

    def test_unapprove_succeeds(self, monkeypatch):
        """Unapprove posts to the unapprove endpoint with no review precondition."""
        monkeypatch.setenv("GITLAB_TOKEN", "test-token")
        mock_api = MagicMock()
        mock_api.post_status.return_value = 201
        mock_api.current_username.return_value = "reviewer-bot"
        # Verify-after-unapprove (#2081): the /approvals read-back must show the
        # identity ABSENT so the confirmed unapprove stays green.
        mock_api.get_json.side_effect = lambda endpoint: (
            {"approved_by": []} if endpoint.endswith("/approvals") else {"id": 1}
        )
        with patch.object(gitlab_api_mod, "GitLabAPI", return_value=mock_api):
            result = runner.invoke(app, ["review", "unapprove", "org/repo", "7"])
            assert result.exit_code == 0, result.output
            assert "OK unapproved" in result.output
            endpoint = mock_api.post_status.call_args.args[0]
            assert "merge_requests/7/unapprove" in endpoint

    def test_unapprove_api_failure_surfaced(self, monkeypatch):
        monkeypatch.setenv("GITLAB_TOKEN", "test-token")
        mock_api = MagicMock()
        mock_api.post_status.return_value = 404
        with patch.object(gitlab_api_mod, "GitLabAPI", return_value=mock_api):
            result = runner.invoke(app, ["review", "unapprove", "org/repo", "7"])
            assert result.exit_code == 1
            assert "Failed: HTTP 404" in result.output

    # ast-grep-ignore: ac-django-no-pytest-django-db
    @pytest.mark.django_db
    def test_unapprove_blocked_by_on_behalf_gate(self, tmp_path, monkeypatch):
        """Gate ON + no recorded approval → unapprove refuses without an API call (#1013)."""
        monkeypatch.setenv("GITLAB_TOKEN", "test-token")
        # See ``test_approve_blocked_by_on_behalf_gate``: undo the autouse
        # gate-off (DB-home ``on_behalf_post_mode`` via env, #1775) so the mode
        # resolves to its blocking ``DRAFT_OR_ASK`` default.
        monkeypatch.delenv("T3_ON_BEHALF_POST_MODE", raising=False)
        mock_api = MagicMock()
        with patch.object(gitlab_api_mod, "GitLabAPI", return_value=mock_api):
            result = runner.invoke(app, ["review", "unapprove", "org/repo", "7"])
            assert result.exit_code == 1
            assert "approve-on-behalf" in result.output
            mock_api.post_status.assert_not_called()


# -- _require_token helper -----------------------------------------------------


class TestRequireToken:
    def test_post_draft_note_rejected(self, monkeypatch):
        """No GitLab token → refusal even when ``--general`` is passed."""
        monkeypatch.delenv("GITLAB_TOKEN", raising=False)
        with patch.object(utils_run_mod.subprocess, "run") as mock_run:
            mock_run.return_value = MagicMock(stderr="", returncode=1)
            result = runner.invoke(
                app,
                ["review", "post-draft-note", "org/repo", "1", "note", "--general"],
            )
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
