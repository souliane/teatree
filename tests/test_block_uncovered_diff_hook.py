"""Tests for the per-diff-coverage PreToolUse hook (#937, §17.6 gate 12).

Gate 12's detection (``teatree.utils.diff_coverage`` / ``t3 tool
diff-coverage``) shipped correct in #862 but was wired into ZERO
automatic enforcement points — absent from CI, pre-commit and the
``hook_router.py`` ``PreToolUse`` chain. §17.6.3 requires it to "run as
a pre-merge gate ... A PR that triggers either check is returned to
draft automatically". This gate mirrors the sibling Gate-15
(``handle_block_ai_signature``) shape: it intercepts the merge-class
mutations that move a PR toward review/merge — ``gh pr ready`` (a draft
PR being un-drafted) and a non-draft ``gh pr create`` / ``glab mr
create`` — and refuses (``deny``) when ``t3 tool diff-coverage`` reports
an uncovered new line or an unreferenced changed symbol. Reverting the
wiring (the ``_HANDLERS`` registration / the handler returning ``True``)
turns the block tests red — the anti-vacuity guarantee.
"""

import json
import subprocess
from unittest.mock import patch

import hooks.scripts.hook_router as router
from hooks.scripts.hook_router import _is_merge_class_mutation, handle_block_uncovered_diff


class TestMergeClassMutationDetection:
    """The trigger surface: PR moving toward review/merge."""

    def test_gh_pr_ready_is_merge_class(self):
        assert _is_merge_class_mutation({"tool_name": "Bash", "tool_input": {"command": "gh pr ready 42"}}) is True

    def test_non_draft_gh_pr_create_is_merge_class(self):
        cmd = "gh pr create --title t --body b"
        assert _is_merge_class_mutation({"tool_name": "Bash", "tool_input": {"command": cmd}}) is True

    def test_non_draft_glab_mr_create_is_merge_class(self):
        cmd = "glab mr create --title t --description d"
        assert _is_merge_class_mutation({"tool_name": "Bash", "tool_input": {"command": cmd}}) is True

    def test_draft_pr_create_is_not_merge_class(self):
        # A draft PR is not yet under review — the gate fires when it is
        # un-drafted (gh pr ready), not at draft creation.
        cmd = "gh pr create --draft --title t --body b"
        assert _is_merge_class_mutation({"tool_name": "Bash", "tool_input": {"command": cmd}}) is False

    def test_gh_pr_ready_undo_is_not_merge_class(self):
        # `gh pr ready --undo` returns the PR TO draft — that is the gate's
        # remediation, never the thing it should block.
        cmd = "gh pr ready 42 --undo"
        assert _is_merge_class_mutation({"tool_name": "Bash", "tool_input": {"command": cmd}}) is False

    def test_unrelated_command_is_not_merge_class(self):
        assert _is_merge_class_mutation({"tool_name": "Bash", "tool_input": {"command": "ls -la"}}) is False

    def test_non_bash_tool_is_not_merge_class(self):
        assert _is_merge_class_mutation({"tool_name": "Read", "tool_input": {"file_path": "/x"}}) is False


class TestBlocksUncoveredDiff:
    def test_blocks_gh_pr_ready_when_diff_coverage_fails(self, monkeypatch, capsys):
        monkeypatch.setattr(router.shutil, "which", lambda _: "/usr/local/bin/t3")
        rejected = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="Per-diff coverage gate: FAILED\n  uncovered new lines in src/x.py: [3]",
            stderr="",
        )
        with patch.object(router.subprocess, "run", return_value=rejected) as run:
            blocked = handle_block_uncovered_diff({"tool_name": "Bash", "tool_input": {"command": "gh pr ready 42"}})
        assert blocked is True
        out = json.loads(capsys.readouterr().out)
        assert out["permissionDecision"] == "deny"
        assert "gate 12" in out["permissionDecisionReason"]
        assert "uncovered new lines" in out["permissionDecisionReason"]
        # It shelled `t3 tool diff-coverage` — reusing the gate as-is.
        assert run.call_args[0][0][:3] == ["/usr/local/bin/t3", "tool", "diff-coverage"]

    def test_blocks_non_draft_pr_create_on_unreferenced_symbol(self, monkeypatch):
        monkeypatch.setattr(router.shutil, "which", lambda _: "/usr/local/bin/t3")
        rejected = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="Per-diff coverage gate: FAILED", stderr=""
        )
        data = {"tool_name": "Bash", "tool_input": {"command": "gh pr create --title t --body b"}}
        with patch.object(router.subprocess, "run", return_value=rejected):
            assert handle_block_uncovered_diff(data) is True


class TestAllowsCleanCases:
    def test_allows_gh_pr_ready_when_diff_coverage_clean(self, monkeypatch):
        monkeypatch.setattr(router.shutil, "which", lambda _: "/usr/local/bin/t3")
        ok = subprocess.CompletedProcess(args=[], returncode=0, stdout="clean", stderr="")
        with patch.object(router.subprocess, "run", return_value=ok):
            assert (
                handle_block_uncovered_diff({"tool_name": "Bash", "tool_input": {"command": "gh pr ready 42"}}) is False
            )

    def test_noop_when_not_a_merge_class_mutation(self):
        # `git commit` is NOT the gate's trigger (Gate 12 is pre-MERGE,
        # not pre-commit) — no t3 shellout, no block.
        data = {"tool_name": "Bash", "tool_input": {"command": "git commit -m 'x'"}}
        assert handle_block_uncovered_diff(data) is False

    def test_noop_for_draft_pr_create(self, monkeypatch):
        monkeypatch.setattr(router.shutil, "which", lambda _: "/usr/local/bin/t3")
        data = {"tool_name": "Bash", "tool_input": {"command": "gh pr create --draft --title t --body b"}}
        with patch.object(router.subprocess, "run") as run:
            assert handle_block_uncovered_diff(data) is False
        run.assert_not_called()

    def test_fail_open_when_t3_not_on_path(self, monkeypatch):
        monkeypatch.setattr(router.shutil, "which", lambda _: None)
        data = {"tool_name": "Bash", "tool_input": {"command": "gh pr ready 42"}}
        assert handle_block_uncovered_diff(data) is False

    def test_fail_open_when_t3_times_out(self, monkeypatch):
        monkeypatch.setattr(router.shutil, "which", lambda _: "/usr/local/bin/t3")
        with patch.object(router.subprocess, "run", side_effect=subprocess.TimeoutExpired("t3", 30)):
            data = {"tool_name": "Bash", "tool_input": {"command": "gh pr ready 42"}}
            assert handle_block_uncovered_diff(data) is False


class TestRegisteredInPreToolUseChain:
    """Anti-vacuity: the handler must be WIRED, not just defined.

    Reverting the wiring (removing the handler from
    ``_HANDLERS['PreToolUse']``) turns this red — the exact false-
    completion surface #937 closes (a gate that exists but never fires).
    """

    def test_handler_is_registered_in_pretooluse(self):
        assert handle_block_uncovered_diff in router._HANDLERS["PreToolUse"]
