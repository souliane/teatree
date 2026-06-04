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


_FINDING_STDOUT = "AI-signature scan: 1 banned trailer(s)\n  line 3: co-authored-by-model: Co-Authored-By: Claude"
_CLEAN_STDOUT = "AI-signature scan: clean (0 findings)"


class TestBlocksBannedTrailer:
    def test_blocks_gh_pr_create_with_generated_with_footer(self, monkeypatch, capsys):
        monkeypatch.setattr(router.shutil, "which", lambda _: "/usr/local/bin/t3")
        rejected = subprocess.CompletedProcess(args=[], returncode=1, stdout=_FINDING_STDOUT, stderr="")
        with patch.object(router.subprocess, "run", return_value=rejected) as run:
            blocked = handle_block_ai_signature(_gh_pr_create("body\n\nGenerated with [Claude Code]"))
        assert blocked is True
        out = json.loads(capsys.readouterr().out)
        assert out["permissionDecision"] == "deny"
        reason = out["permissionDecisionReason"]
        assert "AI-signature" in reason
        assert "banned trailer in the PR body or commit message" in reason
        assert "scanner error" not in reason
        # The body text was piped to `t3 tool ai-sig-scan -` on stdin.
        assert run.call_args[0][0][:3] == ["/usr/local/bin/t3", "tool", "ai-sig-scan"]
        assert "Generated with" in run.call_args[1]["input"]

    def test_blocks_git_commit_with_co_authored_by_model(self, monkeypatch):
        monkeypatch.setattr(router.shutil, "which", lambda _: "/usr/local/bin/t3")
        data = {
            "tool_name": "Bash",
            "tool_input": {"command": "git commit -m 'fix: x\n\nCo-Authored-By: Claude <noreply@anthropic.com>'"},
        }
        rejected = subprocess.CompletedProcess(args=[], returncode=1, stdout=_FINDING_STDOUT, stderr="")
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
        rejected = subprocess.CompletedProcess(args=[], returncode=1, stdout=_FINDING_STDOUT, stderr="")
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
        rejected = subprocess.CompletedProcess(args=[], returncode=1, stdout=_FINDING_STDOUT, stderr="")
        with patch.object(router.subprocess, "run", return_value=rejected) as run:
            blocked = handle_block_ai_signature(data)
        assert blocked is True
        assert "Co-Authored-By" in run.call_args[1]["input"]

    def test_gh_pr_create_body_file_is_read_and_blocked(self, monkeypatch, tmp_path):
        body = tmp_path / "pr_body.md"
        body.write_text(self._TRAILER, encoding="utf-8")
        monkeypatch.setattr(router.shutil, "which", lambda _: "/usr/local/bin/t3")
        data = {"tool_name": "Bash", "tool_input": {"command": f"gh pr create --title t --body-file {body}"}}
        rejected = subprocess.CompletedProcess(args=[], returncode=1, stdout=_FINDING_STDOUT, stderr="")
        with patch.object(router.subprocess, "run", return_value=rejected) as run:
            assert handle_block_ai_signature(data) is True
        assert "Co-Authored-By" in run.call_args[1]["input"]

    def test_glab_mr_description_file_is_read(self, tmp_path):
        # glab's FILE flag is ``--description-file`` (``--description`` takes an
        # inline string, not a path). The shared canonical parser reads the
        # ``--description-file`` body file; the previous hand-rolled regex
        # mistakenly treated ``--description <path>`` as a file too.
        desc = tmp_path / "mr.md"
        desc.write_text(self._TRAILER, encoding="utf-8")
        data = {
            "tool_name": "Bash",
            "tool_input": {"command": f"glab mr create --title t --description-file {desc}"},
        }
        payload = _extract_ai_sig_payload(data)
        assert payload is not None
        assert "Co-Authored-By" in payload

    def test_git_commit_minus_c_reuse_ref_is_not_a_body_surface(self, tmp_path):
        # ``git commit -C <commit>`` REUSES the message of an existing commit
        # OBJECT (by ref/SHA) — it does NOT read a file path, so there is no
        # hook-readable body to scan and the command is not a body surface. The
        # previous hand-rolled ``-[FC]`` regex misread ``-C`` as a file path; the
        # canonical parser correctly scopes file resolution to ``-F``.
        data = {"tool_name": "Bash", "tool_input": {"command": "git commit -C HEAD~1"}}
        assert _extract_ai_sig_payload(data) is None

    def test_git_commit_glued_minus_f_no_separator_is_blocked(self, monkeypatch, tmp_path):
        """Glued ``git commit -F<path>`` (no separator) must be scanned.

        #862 cold-review residual: ``-F<path>`` with no space or ``=`` is
        valid git getopt; the space/``=``-only matcher let a glued short
        flag carrying a banned trailer slip past — this closes that hole.
        """
        msg = tmp_path / "GLUED_MSG"
        msg.write_text(self._TRAILER, encoding="utf-8")
        monkeypatch.setattr(router.shutil, "which", lambda _: "/usr/local/bin/t3")
        data = {"tool_name": "Bash", "tool_input": {"command": f"git commit -F{msg}"}}
        rejected = subprocess.CompletedProcess(args=[], returncode=1, stdout=_FINDING_STDOUT, stderr="")
        with patch.object(router.subprocess, "run", return_value=rejected) as run:
            blocked = handle_block_ai_signature(data)
        assert blocked is True
        assert "Co-Authored-By" in run.call_args[1]["input"]

    def test_git_commit_glued_minus_c_no_separator_is_not_a_body_surface(self, tmp_path):
        # Glued ``git commit -CHEAD~1`` is the same reuse-by-ref semantics as the
        # spaced form (see ``test_git_commit_minus_c_reuse_ref_is_not_a_body_surface``).
        data = {"tool_name": "Bash", "tool_input": {"command": "git commit -CHEAD~1"}}
        assert _extract_ai_sig_payload(data) is None

    def test_existing_space_and_equals_short_forms_still_blocked(self, tmp_path):
        """Glued-form fix must not regress ``-F path`` / ``-F=path``."""
        msg = tmp_path / "msg"
        msg.write_text(self._TRAILER, encoding="utf-8")
        for cmd in (f"git commit -F {msg}", f"git commit -F={msg}"):
            data = {"tool_name": "Bash", "tool_input": {"command": cmd}}
            payload = _extract_ai_sig_payload(data)
            assert payload is not None, cmd
            assert "Co-Authored-By" in payload, cmd

    def test_clean_glued_minus_f_is_allowed(self, monkeypatch, tmp_path):
        body = tmp_path / "clean_glued.md"
        body.write_text("fix: real change\n\nClean.\n\nRelates-to #836\n", encoding="utf-8")
        monkeypatch.setattr(router.shutil, "which", lambda _: "/usr/local/bin/t3")
        data = {"tool_name": "Bash", "tool_input": {"command": f"git commit -F{body}"}}
        ok = subprocess.CompletedProcess(args=[], returncode=0, stdout="clean", stderr="")
        with patch.object(router.subprocess, "run", return_value=ok):
            assert handle_block_ai_signature(data) is False

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


class TestScannerErrorIsDistinguishedFromFinding:
    """#1884 — a scanner crash must NOT masquerade as a banned-trailer finding.

    The scanner (``scripts/ai_signature_scan.py``) exits 1 BOTH on a real
    finding AND on a crash (a missing/unreadable file argument → typer
    traceback → exit 1, no ``AI-signature scan:`` summary on stdout). The old
    gate did ``if returncode != 0: deny('…banned trailer…')`` — so a crash
    produced a false DENY carrying the LYING "banned trailer found" message.

    This is a SECURITY gate (it prevents publishing AI signatures under the
    user's identity). The safe posture on a scanner error is FAIL CLOSED with
    a clear "scanner error, not a finding" message — block, but report the
    real reason; never silently allow an unscanned publish, never claim a
    finding that did not happen. (Contrast the sibling coverage gate, which
    correctly fails OPEN — a broken env must not block a merge there.)
    """

    _CRASH_STDERR = "Traceback (most recent call last):\n  FileNotFoundError: [Errno 2] No such file"

    def test_scanner_crash_fails_closed_with_scanner_error_not_finding_message(self, monkeypatch, capsys):
        monkeypatch.setattr(router.shutil, "which", lambda _: "/usr/local/bin/t3")
        crashed = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr=self._CRASH_STDERR)
        with patch.object(router.subprocess, "run", return_value=crashed):
            blocked = handle_block_ai_signature(_gh_pr_create("a clean description\n\nRelates-to #836"))
        assert blocked is True
        out = json.loads(capsys.readouterr().out)
        assert out["permissionDecision"] == "deny"
        reason = out["permissionDecisionReason"]
        assert "scanner error" in reason.lower()
        # The crash must NOT be reported as a real finding.
        assert "banned trailer in the PR body or commit message" not in reason

    def test_scanner_nonzero_without_summary_fails_closed(self, monkeypatch, capsys):
        # exit 2 (typer/argparse usage error) with no well-formed summary is a
        # tool error, not a finding — fail closed, clear message.
        monkeypatch.setattr(router.shutil, "which", lambda _: "/usr/local/bin/t3")
        errored = subprocess.CompletedProcess(args=[], returncode=2, stdout="Usage: …", stderr="error: bad")
        with patch.object(router.subprocess, "run", return_value=errored):
            blocked = handle_block_ai_signature(_gh_pr_create("a clean description"))
        assert blocked is True
        reason = json.loads(capsys.readouterr().out)["permissionDecisionReason"]
        assert "scanner error" in reason.lower()
        assert "banned trailer in the PR body or commit message" not in reason

    def test_real_finding_still_denies_with_finding_message(self, monkeypatch, capsys):
        monkeypatch.setattr(router.shutil, "which", lambda _: "/usr/local/bin/t3")
        rejected = subprocess.CompletedProcess(args=[], returncode=1, stdout=_FINDING_STDOUT, stderr="")
        with patch.object(router.subprocess, "run", return_value=rejected):
            blocked = handle_block_ai_signature(_gh_pr_create("body\n\nGenerated with [Claude Code]"))
        assert blocked is True
        reason = json.loads(capsys.readouterr().out)["permissionDecisionReason"]
        assert "banned trailer in the PR body or commit message" in reason
        assert "scanner error" not in reason.lower()

    def test_clean_scan_allows(self, monkeypatch):
        monkeypatch.setattr(router.shutil, "which", lambda _: "/usr/local/bin/t3")
        ok = subprocess.CompletedProcess(args=[], returncode=0, stdout=_CLEAN_STDOUT, stderr="")
        with patch.object(router.subprocess, "run", return_value=ok):
            assert handle_block_ai_signature(_gh_pr_create("a clean description\n\nRelates-to #836")) is False


class TestAllowsCleanCases:
    def test_allows_clean_pr_body(self, monkeypatch):
        monkeypatch.setattr(router.shutil, "which", lambda _: "/usr/local/bin/t3")
        ok = subprocess.CompletedProcess(args=[], returncode=0, stdout=_CLEAN_STDOUT, stderr="")
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
