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
            "bash -c 'git add -A'",
            "/bin/zsh -lc 'git add .'",
            "cd /repo && sh -c 'git add --all' && echo done",
            "timeout 60 bash -lc 'git -C /repo add -A'",
            "bash -c \"bash -c 'git add -A'\"",
            "if true; then bash -c 'git add -A'; fi",
            "for f in a b; do bash -c 'git add -A'; done",
            "! bash -c 'git add -A'",
            "{ bash -c 'git add -A'; }",
            "( bash -c 'git add -A' )",
            "ksh -c 'git add -A'",
            "bash -c $'git add -A'",
            "bash --rcfile /dev/null -c 'git add -A'",
            "sudo bash -c 'git add -A'",
            "exec bash -c 'git add .'",
            "sudo git add -A",
            "timeout 60 git add -A",
            "exec git add -A",
            "if git add -A; then echo staged; fi",
            "while true; do git add .; done",
            "nice -n 5 bash -c 'git add -A'",
            "sudo -u deploy bash -c 'git add -A'",
            "env -i bash -c 'git add -A'",
            "env -u HOME bash -lc 'git add .'",
            "sudo -u deploy nice -n 5 bash -c 'git add .'",
            "nice -n 5 git add -A",
            "sudo -u root git add .",
            "env -i git add --all",
            "sudo -n git add -A",
            "sudo -n bash -c 'git add -A'",
            "sudo -n -u deploy git add .",
            "sudo -C 3 git add -A",
            "sudo -D /tmp git add -A",
            "sudo -p prompt: bash -c 'git add -A'",
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
            "bash -c 'git add src/app/models.py'",
            "bash -c 'git add -p'",
            "bash ./stage.sh",
            "git commit -m \"bash -c 'git add -A'\"",
            "echo \"zsh -lc 'git add .'\"",
            "python3 -c \"print('git add -A')\"",
            "echo $(git status --short)",
            "if git add src/a.py; then echo staged; fi",
            "sudo git add src/a.py",
            "timeout 60 git add src/a.py",
            "exec git add -p",
            "for f in a b; do bash -c 'git add \"$f\"'; done",
            "bash -c 'echo hello'",
            "nice -n 5 git add src/a.py",
            "sudo -u root git status",
            "env -i bash -c 'echo hi'",
            "nice -n 5 bash -c 'echo hi'",
            "sudo -n git status",
            "sudo -n bash -c 'echo hi'",
            "sudo -C 3 git status",
            "sudo -D /tmp bash -c 'echo hi'",
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

    def test_a_wrapped_script_in_a_heredoc_body_is_text(self) -> None:
        command = "gh issue comment 1 --body-file - <<'EOF'\nbash -c 'git add -A'\nEOF\n"
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
    def test_a_token_inside_the_wrapped_script_allows(self) -> None:
        blocked, _payload = _run("bash -c 'git add -A # [add-all-ok: first commit of a scaffold]'")
        assert blocked is False

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
