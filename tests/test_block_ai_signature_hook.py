"""Tests for the block-ai-signature PreToolUse hook (#836 §17.6 gate 15).

The "No AI Signature on Posts Made on the User's Behalf" rule lived only
as prose in /t3:rules and was UNENFORCED at the PR-body / commit-message
layer — PR #831 leaked the ``Generated with [Claude Code]`` trailer,
caught only by cold review. This gate intercepts ``gh pr create`` /
``glab mr create`` / ``git commit`` / the MR-MCP tools and refuses the
mutation when the body or message carries a banned trailer. It runs at
the same pre-merge layer as the draft-lock and structured-question gates.
"""

import json
import subprocess
from unittest.mock import patch

import hooks.scripts.hook_router as router
from hooks.scripts.hook_router import _extract_ai_sig_payload, handle_block_ai_signature


def _gh_pr_create(body: str) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": f"gh pr create --title 't' --body '{body}'"}}


class TestBlocksBannedTrailer:
    def test_blocks_gh_pr_create_with_generated_with_footer(self, monkeypatch, capsys):
        monkeypatch.setattr(router.shutil, "which", lambda _: "/usr/local/bin/t3")
        rejected = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="AI-signature scan: 1 banned trailer(s)", stderr=""
        )
        with patch.object(router.subprocess, "run", return_value=rejected) as run:
            blocked = handle_block_ai_signature(_gh_pr_create("body\n\nGenerated with [Claude Code]"))
        assert blocked is True
        out = json.loads(capsys.readouterr().out)
        assert out["permissionDecision"] == "deny"
        assert "AI-signature" in out["permissionDecisionReason"]
        # The body text was piped to `t3 tool ai-sig-scan -` on stdin.
        assert run.call_args[0][0][:3] == ["/usr/local/bin/t3", "tool", "ai-sig-scan"]
        assert "Generated with" in run.call_args[1]["input"]

    def test_blocks_git_commit_with_co_authored_by_model(self, monkeypatch):
        monkeypatch.setattr(router.shutil, "which", lambda _: "/usr/local/bin/t3")
        data = {
            "tool_name": "Bash",
            "tool_input": {"command": "git commit -m 'fix: x\n\nCo-Authored-By: Claude <noreply@anthropic.com>'"},
        }
        rejected = subprocess.CompletedProcess(args=[], returncode=1, stdout="banned", stderr="")
        with patch.object(router.subprocess, "run", return_value=rejected) as run:
            blocked = handle_block_ai_signature(data)
        assert blocked is True
        assert "Co-Authored-By" in run.call_args[1]["input"]

    def test_blocks_mr_mcp_tool_body(self, monkeypatch):
        monkeypatch.setattr(router.shutil, "which", lambda _: "/usr/local/bin/t3")
        data = {
            "tool_name": "mcp__github__create_pull_request",
            "tool_input": {"title": "t", "body": "desc\n\n\U0001f916 Generated with [Claude Code]"},
        }
        rejected = subprocess.CompletedProcess(args=[], returncode=1, stdout="banned", stderr="")
        with patch.object(router.subprocess, "run", return_value=rejected):
            assert handle_block_ai_signature(data) is True


class TestFileBasedMessageIsScanned:
    """Cold-review finding 2 — file-based message bypass.

    The standard multi-line file path — ``git commit -F``, ``gh pr
    create --body-file``, ``glab mr create --description <file>``,
    ``git commit -C`` — must be read and scanned. This is exactly
    #831's shape (a banned trailer in a multi-line body file); leaving
    it unscanned defeats the gate's stated guarantee.
    """

    _TRAILER = "fix: x\n\nbody\n\nCo-Authored-By: Claude <noreply@anthropic.com>\n"

    def test_git_commit_minus_f_file_is_read_and_blocked(self, monkeypatch, tmp_path, capsys):
        msg = tmp_path / "COMMIT_MSG"
        msg.write_text(self._TRAILER, encoding="utf-8")
        monkeypatch.setattr(router.shutil, "which", lambda _: "/usr/local/bin/t3")
        data = {"tool_name": "Bash", "tool_input": {"command": f"git commit -F {msg}"}}
        rejected = subprocess.CompletedProcess(args=[], returncode=1, stdout="banned", stderr="")
        with patch.object(router.subprocess, "run", return_value=rejected) as run:
            blocked = handle_block_ai_signature(data)
        assert blocked is True
        assert "Co-Authored-By" in run.call_args[1]["input"]

    def test_gh_pr_create_body_file_is_read_and_blocked(self, monkeypatch, tmp_path):
        body = tmp_path / "pr_body.md"
        body.write_text(self._TRAILER, encoding="utf-8")
        monkeypatch.setattr(router.shutil, "which", lambda _: "/usr/local/bin/t3")
        data = {"tool_name": "Bash", "tool_input": {"command": f"gh pr create --title t --body-file {body}"}}
        rejected = subprocess.CompletedProcess(args=[], returncode=1, stdout="banned", stderr="")
        with patch.object(router.subprocess, "run", return_value=rejected) as run:
            assert handle_block_ai_signature(data) is True
        assert "Co-Authored-By" in run.call_args[1]["input"]

    def test_glab_mr_description_file_is_read(self, tmp_path):
        desc = tmp_path / "mr.md"
        desc.write_text(self._TRAILER, encoding="utf-8")
        data = {"tool_name": "Bash", "tool_input": {"command": f"glab mr create --title t --description {desc}"}}
        payload = _extract_ai_sig_payload(data)
        assert payload is not None
        assert "Co-Authored-By" in payload

    def test_git_commit_minus_c_reuse_file_is_read(self, tmp_path):
        msg = tmp_path / "src_commit"
        msg.write_text(self._TRAILER, encoding="utf-8")
        data = {"tool_name": "Bash", "tool_input": {"command": f"git commit -C {msg}"}}
        payload = _extract_ai_sig_payload(data)
        assert payload is not None
        assert "Co-Authored-By" in payload

    def test_clean_body_file_is_allowed(self, monkeypatch, tmp_path):
        body = tmp_path / "clean.md"
        body.write_text("fix: real change\n\nA clean description.\n\nRelates-to #836\n", encoding="utf-8")
        monkeypatch.setattr(router.shutil, "which", lambda _: "/usr/local/bin/t3")
        data = {"tool_name": "Bash", "tool_input": {"command": f"git commit -F {body}"}}
        ok = subprocess.CompletedProcess(args=[], returncode=0, stdout="clean", stderr="")
        with patch.object(router.subprocess, "run", return_value=ok):
            assert handle_block_ai_signature(data) is False

    def test_nonexistent_message_file_fails_open_no_crash(self):
        data = {"tool_name": "Bash", "tool_input": {"command": "git commit -F /no/such/file"}}
        # File-missing → fail open (None), never raise.
        assert _extract_ai_sig_payload(data) is None

    def test_binary_message_file_fails_open_no_crash(self, tmp_path):
        binf = tmp_path / "binary.bin"
        binf.write_bytes(b"\x00\xff\xfe binary \x00 not text")
        data = {"tool_name": "Bash", "tool_input": {"command": f"git commit -F {binf}"}}
        # Undecodable file → fail open (None), never raise.
        assert _extract_ai_sig_payload(data) is None


class TestAllowsCleanCases:
    def test_allows_clean_pr_body(self, monkeypatch):
        monkeypatch.setattr(router.shutil, "which", lambda _: "/usr/local/bin/t3")
        ok = subprocess.CompletedProcess(args=[], returncode=0, stdout="clean", stderr="")
        with patch.object(router.subprocess, "run", return_value=ok):
            assert handle_block_ai_signature(_gh_pr_create("a clean description\n\nRelates-to #836")) is False

    def test_noop_when_not_a_pr_or_commit_command(self, monkeypatch):
        assert handle_block_ai_signature({"tool_name": "Bash", "tool_input": {"command": "ls -la"}}) is False

    def test_noop_when_git_commit_has_no_inline_message(self, monkeypatch):
        # `git commit` opening an editor (no -m) has no payload to scan here.
        data = {"tool_name": "Bash", "tool_input": {"command": "git commit"}}
        assert handle_block_ai_signature(data) is False

    def test_fail_open_when_t3_not_on_path(self, monkeypatch):
        monkeypatch.setattr(router.shutil, "which", lambda _: None)
        assert handle_block_ai_signature(_gh_pr_create("body\n\nGenerated with [Claude Code]")) is False
