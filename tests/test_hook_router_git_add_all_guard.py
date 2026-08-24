# test-path: cross-cutting
# Exercises the hooks/scripts/git_add_all_guard.py PreToolUse handler wired into
# hook_router.py (no src/teatree mirror), so it spans packages.
"""``git add -A`` / ``git add .`` must be refused before anything is staged (#4093).

The whole-tree stage keeps sweeping unrelated files into commits — a scratch
file an agent wrote next to the code it was editing, and, in a shared worktree,
another agent's in-progress edits committed under the wrong authorship. The only
thing that caught it was ``tests/test_repo_root_minimal.py``: after the commit
existed, and only for a file that landed at the repo ROOT.

The gate is deliberately narrow, so both directions are pinned here. It denies
the whole-tree sweep; it leaves ``git add <explicit paths>``, ``git add -p`` and
``git add -u`` (tracked files only — no untracked sweep) alone, and it does not
fire on the phrase inside a commit message, a heredoc body, or a doc string.
"""

import json
from io import StringIO
from unittest.mock import patch

import pytest

import hooks.scripts.hook_router as router


def _bash_event(command: str) -> dict:
    return {"session_id": "sess-add-all", "tool_name": "Bash", "tool_input": {"command": command}}


def _run(command: str) -> tuple[bool, dict | None]:
    buf = StringIO()
    with patch("sys.stdout", buf):
        blocked = router.handle_block_git_add_all(_bash_event(command))
    raw = buf.getvalue().strip()
    return blocked, (json.loads(raw) if raw else None)


def _chain_denies(command: str) -> bool:
    """True iff ANY registered PreToolUse handler refuses *command*."""
    buf = StringIO()
    with patch("sys.stdout", buf):
        return any(handler(_bash_event(command)) for handler in router._HANDLERS["PreToolUse"])


class TestRegisteredChainRefusesTheWholeTreeStage:
    """The anti-vacuous proof: BEFORE this gate no registered handler said no."""

    @pytest.mark.parametrize("command", ["git add -A", "git add --all", "git add ."])
    def test_some_registered_handler_denies(self, command: str) -> None:
        assert _chain_denies(command) is True

    def test_explicit_paths_still_pass_the_whole_chain(self) -> None:
        assert _chain_denies("git add src/app/models.py") is False


class TestWholeTreeStageIsDenied:
    @pytest.mark.parametrize(
        "command",
        [
            "git add -A",
            "git add --all",
            "git add .",
            "git add -A .",
            "git -C /repo add -A",
            "git add --no-ignore-removal .",
            "cd src && git add -A && git commit -m 'wip'",
        ],
    )
    def test_denied(self, command: str) -> None:
        blocked, payload = _run(command)
        assert blocked is True
        assert payload is not None
        assert payload["permissionDecision"] == "deny"
        assert "git add <path>" in payload["permissionDecisionReason"]

    def test_deny_message_names_the_explicit_form_and_the_scratch_dir(self) -> None:
        from hooks.scripts.git_add_all_guard import deny_reason  # noqa: PLC0415 — the module under test

        reason = deny_reason()
        assert "git add <path>" in reason
        assert "scratch" in reason.lower()
        assert "[add-all-ok:" in reason


class TestNarrowlyScoped:
    @pytest.mark.parametrize(
        "command",
        [
            "git add src/app/models.py tests/test_models.py",
            "git add -p",
            "git add -u",
            "git add -u src/",
            "git status --short",
            "git commit -m 'never run git add -A again'",
            "grep -rn 'git add -A' docs/",
            'gh pr create --body "we used to git add . here"',
            "echo 'git add -A' >> notes.md",
            "echo git add -A",
            "git add -n .",
            "git add --dry-run .",
            # A dry run stages nothing, so the sweep flag it is clustered with
            # cannot sweep either — the no-sweep test has to run FIRST (#4127).
            "git add -An",
            "git add -nA",
            "git add --all --dry-run",
        ],
    )
    def test_allowed(self, command: str) -> None:
        blocked, payload = _run(command)
        assert blocked is False
        assert payload is None

    def test_heredoc_body_mentioning_the_sweep_is_not_an_invocation(self) -> None:
        command = "gh issue comment 1 --body-file - <<'EOF'\nthe fix: stop running git add -A\nEOF\n"
        blocked, payload = _run(command)
        assert blocked is False
        assert payload is None

    def test_non_bash_tool_is_ignored(self) -> None:
        blocked, payload = _run("git add -A")
        assert blocked is True
        event = {"tool_name": "Edit", "tool_input": {"file_path": "/x", "new_string": "git add -A"}}
        assert router.handle_block_git_add_all(event) is False
        assert payload is not None


class TestNeverLockout:
    def test_per_call_token_allows(self) -> None:
        blocked, payload = _run("git add -A  # [add-all-ok: first commit on a scaffolded dir]")
        assert blocked is False
        assert payload is None

    def test_kill_switch_disables_the_gate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            router,
            "_teatree_bool_setting",
            lambda key, default=True: False if key == "git_add_all_gate_enabled" else default,
        )
        blocked, payload = _run("git add -A")
        assert blocked is False
        assert payload is None
