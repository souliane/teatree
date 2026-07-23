# test-path: cross-cutting — drives the hooks/scripts/coverage_gate.py PreToolUse gate; the
# teatree.utils.diff_coverage import is only the byte-identity drift guard (#3521), no src/teatree/ mirror.
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
from pathlib import Path
from unittest.mock import patch

import hooks.scripts.hook_router as router
from hooks.scripts.coverage_gate import diff_coverage_finding
from hooks.scripts.hook_router import _is_merge_class_mutation, handle_block_uncovered_diff
from teatree.utils.diff_coverage import UNREFERENCED_SYMBOL_IMPORT_HINT


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


def _finding_json(*, uncovered: list[dict] | None = None, symbols: list[str] | None = None) -> str:
    return json.dumps(
        {
            "passes": False,
            "uncovered": uncovered if uncovered is not None else [{"path": "src/x.py", "lines": [3]}],
            "unreferenced_symbols": symbols or [],
        }
    )


class TestBlocksUncoveredDiff:
    def test_blocks_gh_pr_ready_when_diff_coverage_fails(self, monkeypatch, capsys):
        monkeypatch.setattr(router.shutil, "which", lambda _: "/usr/local/bin/t3")
        rejected = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout=_finding_json(),
            stderr="",
        )
        with patch.object(router.subprocess, "run", return_value=rejected) as run:
            blocked = handle_block_uncovered_diff({"tool_name": "Bash", "tool_input": {"command": "gh pr ready 42"}})
        assert blocked is True
        out = json.loads(capsys.readouterr().out)
        assert out["permissionDecision"] == "deny"
        assert "gate 12" in out["permissionDecisionReason"]
        # It shelled `t3 tool diff-coverage --json` — reusing the gate as-is.
        assert run.call_args[0][0][:3] == ["/usr/local/bin/t3", "tool", "diff-coverage"]
        assert "--json" in run.call_args[0][0]

    def test_blocks_non_draft_pr_create_on_unreferenced_symbol(self, monkeypatch):
        monkeypatch.setattr(router.shutil, "which", lambda _: "/usr/local/bin/t3")
        rejected = subprocess.CompletedProcess(
            args=[], returncode=1, stdout=_finding_json(uncovered=[], symbols=["build_widget"]), stderr=""
        )
        data = {"tool_name": "Bash", "tool_input": {"command": "gh pr create --title t --body b"}}
        with patch.object(router.subprocess, "run", return_value=rejected):
            assert handle_block_uncovered_diff(data) is True


class TestFindingNamesImportWorkaround:
    """The deny reason names the import-only workaround, not a misleading "reference it" (#3521)."""

    def test_unreferenced_symbol_finding_names_the_import_only_workaround(self) -> None:
        finding = diff_coverage_finding(_finding_json(uncovered=[], symbols=["build_widget"]))
        assert finding is not None
        assert "import statements only" in finding
        assert "does not count as a reference" in finding
        assert "from module import symbol" in finding

    def test_import_workaround_absent_from_pure_uncovered_line_finding(self) -> None:
        finding = diff_coverage_finding(_finding_json(uncovered=[{"path": "src/x.py", "lines": [3]}], symbols=[]))
        assert finding is not None
        assert "import statements only" not in finding

    def test_hook_finding_hint_is_byte_identical_to_the_canonical_source(self) -> None:
        finding = diff_coverage_finding(_finding_json(uncovered=[], symbols=["build_widget"]))
        assert finding is not None
        assert UNREFERENCED_SYMBOL_IMPORT_HINT in finding


class TestFailsOpenOnBrokenSubprocess:
    """The documented contract: DENY only on a successfully-computed finding.

    A crash (``ModuleNotFoundError: No module named 'coverage'`` — the
    dev-only ``coverage`` dep is absent from the installed ``t3`` tool
    env), an import error, a nonzero exit with no parseable JSON, or
    malformed JSON must FAIL OPEN (return ``False``). Treating a crash as
    a coverage *finding* and denying — the #122 lockout — turns every
    ``gh pr create`` into a deny. Reverting the fix (back to
    ``if result.returncode != 0: deny``) turns these RED.
    """

    def test_fail_open_on_module_not_found_crash(self, monkeypatch):
        # The exact #122 shape: coverage missing → traceback on stderr,
        # empty stdout, exit 1. The current (buggy) gate denied here.
        monkeypatch.setattr(router.shutil, "which", lambda _: "/usr/local/bin/t3")
        crashed = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr="Traceback (most recent call last):\nModuleNotFoundError: No module named 'coverage'",
        )
        data = {"tool_name": "Bash", "tool_input": {"command": "gh pr create --title t --body b"}}
        with patch.object(router.subprocess, "run", return_value=crashed):
            assert handle_block_uncovered_diff(data) is False

    def test_fail_open_on_nonzero_with_unparseable_stdout(self, monkeypatch):
        monkeypatch.setattr(router.shutil, "which", lambda _: "/usr/local/bin/t3")
        garbage = subprocess.CompletedProcess(args=[], returncode=2, stdout="some non-json error text", stderr="boom")
        data = {"tool_name": "Bash", "tool_input": {"command": "gh pr ready 42"}}
        with patch.object(router.subprocess, "run", return_value=garbage):
            assert handle_block_uncovered_diff(data) is False

    def test_fail_open_on_malformed_json(self, monkeypatch):
        monkeypatch.setattr(router.shutil, "which", lambda _: "/usr/local/bin/t3")
        truncated = subprocess.CompletedProcess(args=[], returncode=1, stdout='{"passes": fal', stderr="")
        data = {"tool_name": "Bash", "tool_input": {"command": "gh pr ready 42"}}
        with patch.object(router.subprocess, "run", return_value=truncated):
            assert handle_block_uncovered_diff(data) is False


class TestAllowsCleanCases:
    def test_allows_gh_pr_ready_when_diff_coverage_clean(self, monkeypatch):
        monkeypatch.setattr(router.shutil, "which", lambda _: "/usr/local/bin/t3")
        clean = json.dumps({"passes": True, "uncovered": [], "unreferenced_symbols": []})
        ok = subprocess.CompletedProcess(args=[], returncode=0, stdout=clean, stderr="")
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


class TestMeasuresTheGatedCommandsWorktree:
    """Measure the worktree the gated command targets, not the session cwd.

    Anti-vacuity: the gate keys ``t3 tool diff-coverage`` to the worktree the
    gated command TARGETS (its own leading ``cd``), never the cold hook's
    inherited session cwd.

    A cross-worktree ship — the session cwd is worktree Y, but the command ships
    worktree X via ``cd X && gh pr create`` — must run ``t3 tool diff-coverage``
    against X. Reverting the cwd-resolution fix measures Y and flags X's PR with
    Y's unrelated uncovered lines (the ``wire.py`` false-flag).
    """

    def _worktree(self, root: Path, name: str) -> Path:
        wt = root / name
        (wt / ".git").mkdir(parents=True)
        return wt

    def test_measures_the_cd_target_worktree_not_the_session_cwd(self, tmp_path, monkeypatch):
        x = self._worktree(tmp_path, "worktree-x")  # the PR's worktree (cd target)
        y = self._worktree(tmp_path, "worktree-y")  # the session cwd (a DIFFERENT worktree)
        monkeypatch.setattr(router.shutil, "which", lambda _: "/usr/local/bin/t3")
        captured: dict = {}

        def fake_run(argv, **kwargs):
            captured["argv"] = argv
            captured["cwd"] = kwargs.get("cwd")
            return subprocess.CompletedProcess(args=argv, returncode=1, stdout=_finding_json(), stderr="")

        with patch.object(router.subprocess, "run", side_effect=fake_run):
            data = {
                "tool_name": "Bash",
                "tool_input": {"command": f"cd {x} && gh pr create --title t --body b"},
                "cwd": str(y),
            }
            assert handle_block_uncovered_diff(data) is True

        # The gate measured X (the cd target), never the session cwd Y.
        assert "--repo" in captured["argv"]
        repo_arg = captured["argv"][captured["argv"].index("--repo") + 1]
        assert Path(repo_arg).resolve() == x.resolve()
        assert Path(captured["cwd"]).resolve() == x.resolve()
        assert str(y.resolve()) not in captured["argv"]

    def test_falls_back_to_session_cwd_when_command_has_no_leading_cd(self, tmp_path, monkeypatch):
        y = self._worktree(tmp_path, "session-cwd")
        monkeypatch.setattr(router.shutil, "which", lambda _: "/usr/local/bin/t3")
        captured: dict = {}

        def fake_run(argv, **kwargs):
            captured["argv"] = argv
            captured["cwd"] = kwargs.get("cwd")
            clean = json.dumps({"passes": True, "uncovered": [], "unreferenced_symbols": []})
            return subprocess.CompletedProcess(args=argv, returncode=0, stdout=clean, stderr="")

        with patch.object(router.subprocess, "run", side_effect=fake_run):
            data = {
                "tool_name": "Bash",
                "tool_input": {"command": "gh pr create --title t --body b"},
                "cwd": str(y),
            }
            assert handle_block_uncovered_diff(data) is False

        # No leading cd → the gate measures the session cwd's OWN repo (correct
        # when the command runs there), never a bare cwd-less run.
        assert Path(captured["cwd"]).resolve() == y.resolve()
        assert "--repo" in captured["argv"]


class TestRegisteredInPreToolUseChain:
    """Anti-vacuity: the handler must be WIRED, not just defined.

    Reverting the wiring (removing the handler from
    ``_HANDLERS['PreToolUse']``) turns this red — the exact false-
    completion surface #937 closes (a gate that exists but never fires).
    """

    def test_handler_is_registered_in_pretooluse(self):
        assert handle_block_uncovered_diff in router._HANDLERS["PreToolUse"]
