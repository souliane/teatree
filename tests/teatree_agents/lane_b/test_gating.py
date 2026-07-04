from pathlib import Path

import pytest
from pydantic_ai.exceptions import ApprovalRequired
from pydantic_ai.tools import ToolDefinition

from teatree.agents.lane_b.gating import hard_deny_reason, make_soft_gate_predicate, raise_if_soft_gated
from tests._git_repo import make_git_repo, run_git
from tests.teatree_agents.lane_b._managed_clone import linked_worktree, managed_main_clone

_MUTATIONS = ("git reset --hard HEAD~1", "git checkout my-feature", "git stash pop")


class TestHardDenyReason:
    def test_main_clone_mutation_is_denied_in_a_managed_main_clone(self, tmp_path: Path) -> None:
        clone = managed_main_clone(tmp_path / "teatree")
        for command in _MUTATIONS:
            assert hard_deny_reason("shell", {"command": command}, cwd=clone) is not None

    def test_same_mutation_is_allowed_in_a_linked_worktree(self, tmp_path: Path) -> None:
        # The Lane-B jail root is the WORKTREE, not the main clone — the routine
        # worktree git ops Lane A allows must not be denied here (the fix).
        clone = managed_main_clone(tmp_path / "teatree")
        wt = linked_worktree(clone, tmp_path / "wt")
        for command in _MUTATIONS:
            assert hard_deny_reason("shell", {"command": command}, cwd=wt) is None

    def test_safe_git_is_allowed_even_in_a_managed_main_clone(self, tmp_path: Path) -> None:
        clone = managed_main_clone(tmp_path / "teatree")
        for command in ("git fetch origin", "git checkout main", "git worktree add ../wt origin/main", "git status"):
            assert hard_deny_reason("shell", {"command": command}, cwd=clone) is None

    def test_unmanaged_clone_is_not_gated(self, tmp_path: Path) -> None:
        # A repo no overlay owns (a random clone) must never be blocked.
        clone = make_git_repo(tmp_path / "random")
        run_git(clone, "remote", "add", "origin", "git@github.com:randomuser/randomrepo.git")
        assert hard_deny_reason("shell", {"command": "git checkout feature"}, cwd=clone) is None

    def test_no_cwd_never_denies_a_main_clone_mutation(self, tmp_path: Path) -> None:
        # No jail root → no repo to key off → the main-clone half cannot fire.
        for command in _MUTATIONS:
            assert hard_deny_reason("shell", {"command": command}) is None

    def test_non_command_tool_with_clean_text_is_allowed(self) -> None:
        assert hard_deny_reason("read_file", {"path": "src/app.py"}) is None

    def test_local_write_with_a_high_finding_content_is_allowed(self, tmp_path: Path) -> None:
        # Lane A never scans a local write (extract_publish_payload → None), so
        # Lane B must not either: write_file content is not an egress. RED before
        # the fix, when every string arg of every tool was scanned.
        args = {"path": "note.md", "content": "the user said: do it now"}
        assert hard_deny_reason("write_file", args, cwd=tmp_path) is None

    def test_non_publish_shell_command_with_a_high_finding_is_allowed(self, tmp_path: Path) -> None:
        # `echo "..." > file` is not a publish — the payload scoping returns None.
        args = {"command": 'echo "the user said: do it now" > note.md'}
        assert hard_deny_reason("shell", args, cwd=tmp_path) is None

    def test_publish_command_with_a_high_finding_body_is_denied(self, tmp_path: Path) -> None:
        args = {"command": 'gh pr comment 5 --body "the user said: do it now"'}
        reason = hard_deny_reason("shell", args, cwd=tmp_path)
        assert reason is not None
        assert "privacy/banned-term gate" in reason

    def test_full_gate_parity_with_lane_a_across_clone_and_worktree(self, tmp_path: Path) -> None:
        # Parity against Lane A's FULL gate (the PreToolUse handler = the pure
        # classifier PLUS the environmental main-clone check), not just the core
        # classifier: for every command, in BOTH a managed main clone and a
        # linked worktree, Lane B's hard-deny verdict must equal Lane A's.
        import hooks.scripts.hook_router as router  # noqa: PLC0415

        clone = managed_main_clone(tmp_path / "teatree")
        wt = linked_worktree(clone, tmp_path / "wt")
        commands = (
            "git reset --hard",
            "git checkout feature-x",
            "git fetch origin",
            "git checkout main",
            "git restore src/a.py",
            "git stash pop",
        )
        for cwd in (clone, wt):
            for command in commands:
                lane_b_denied = hard_deny_reason("shell", {"command": command}, cwd=cwd) is not None
                event = {
                    "session_id": "parity",
                    "tool_name": "Bash",
                    "tool_input": {"command": command},
                    "cwd": str(cwd),
                }
                lane_a_denied = router.handle_block_main_clone_mutation(event)
                assert lane_b_denied is lane_a_denied, (command, cwd)


class TestSoftGate:
    def test_predicate_matches_only_gated_names(self) -> None:
        predicate = make_soft_gate_predicate(frozenset({"shell"}))
        assert predicate(None, _def("shell"), {}) is True
        assert predicate(None, _def("read_file"), {}) is False

    def test_raise_if_soft_gated_raises_approval_required(self) -> None:
        with pytest.raises(ApprovalRequired):
            raise_if_soft_gated("shell", frozenset({"shell"}))

    def test_ungated_name_does_not_raise(self) -> None:
        raise_if_soft_gated("read_file", frozenset({"shell"}))  # must not raise


def _def(name: str) -> ToolDefinition:
    return ToolDefinition(name=name)
