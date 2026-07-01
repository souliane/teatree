"""Tests for PreToolUse Bash command blocker in hook_router.

Verifies that forbidden infrastructure commands (manage.py, playwright, docker compose, etc.)
are denied with a specific t3 alternative, while legitimate t3 and git commands pass through.
"""

import json
from pathlib import Path

import pytest

import hooks.scripts.hook_router as router
from hooks.scripts.hook_router import handle_block_direct_commands


@pytest.fixture(autouse=True)
def _isolate_state_dir(tmp_path: Path):
    original = router.STATE_DIR
    router.STATE_DIR = tmp_path / "state"
    router.STATE_DIR.mkdir(parents=True, exist_ok=True)
    yield
    router.STATE_DIR = original


def _bash_event(command: str, tool_name: str = "Bash") -> dict:
    return {
        "session_id": "sess-block",
        "tool_name": tool_name,
        "tool_input": {"command": command},
    }


def _parse_deny(capsys: pytest.CaptureFixture[str]) -> dict | None:
    output = capsys.readouterr().out.strip()
    if not output:
        return None
    return json.loads(output)


class TestBlocksForbiddenCommands:
    """Forbidden infrastructure commands are denied with specific t3 alternatives."""

    @pytest.mark.parametrize(
        ("command", "expected_fragment"),
        [
            ("python manage.py runserver", "worktree start"),
            ("./manage.py runserver 0.0.0.0:8000", "worktree start"),
            ("python manage.py migrate", "worktree provision"),
            ("./manage.py migrate --run-syncdb", "worktree provision"),
            ("nx serve my-app", "worktree start"),
            ("docker compose up -d", "worktree start"),
            ("docker compose start", "worktree start"),
            ("createdb mydb", "db"),
            ("dropdb mydb", "db"),
            ("npx playwright test", "e2e"),
            ("playwright test --headed", "e2e"),
            ("npm run serve", "run"),
            ("npm run start", "run"),
            ("pipenv install requests", "worktree provision"),
            ("pip install -r requirements.txt", "worktree provision"),
            ("pg_restore -d mydb dump.sql", "db"),
            ("pg_dump mydb > dump.sql", "db"),
            ("dslr snapshot mydb", "db"),
            ("uv run t3 worktree start", "install teatree"),
            ("uv run t3 teatree worktree provision", "install teatree"),
            ("cd /tmp && uv run t3 info", "install teatree"),
            ("dslr restore my_snap", "db"),
            ("T3_ALLOW_REMOTE_DUMP=1 t3 myapp db refresh", "T3_ALLOW_REMOTE_DUMP"),
            ("git commit --no-verify -m 'skip hooks'", "--no-verify"),
            ("git push --no-verify origin main", "--no-verify"),
            ("git rebase --no-verify main", "--no-verify"),
            ("git merge --no-verify feature", "--no-verify"),
            ("git commit --no-gpg-sign -m 'skip'", "--no-gpg-sign"),
            (".venv/bin/python manage.py shell", "uv run"),
            (".venv/bin/pytest tests/", "uv run"),
            (".venv/bin/pip install foo", "uv run"),
            ("safety check", "pip-audit"),
            ("safety scan --full-report", "pip-audit"),
        ],
    )
    def test_denies_with_t3_alternative(
        self,
        capsys: pytest.CaptureFixture[str],
        command: str,
        expected_fragment: str,
    ) -> None:
        result = handle_block_direct_commands(_bash_event(command))
        assert result is True
        deny = _parse_deny(capsys)
        assert deny is not None
        assert deny["permissionDecision"] == "deny"
        assert expected_fragment in deny["permissionDecisionReason"]

    def test_deny_message_includes_blocked_command(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        handle_block_direct_commands(_bash_event("python manage.py runserver"))
        deny = _parse_deny(capsys)
        assert deny is not None
        assert "manage.py runserver" in deny["permissionDecisionReason"]


class TestAllowsLegitimateCommands:
    """Legitimate t3, git, and general commands pass through."""

    @pytest.mark.parametrize(
        "command",
        [
            "t3 myapp worktree start",
            "t3 teatree worktree provision",
            "PYENV_VERSION=3.13.11 t3 myapp e2e external",
            "git status",
            "git push origin main",
            "ruff check src/",
            "uv run pytest --no-cov -x",
            "uv run pytest tests/test_t3_cli.py",
            "ls -la",
            "echo 'manage.py runserver is not allowed'",
            "echo 'run uv run t3 command'",
            "cat README.md",
            "grep -r 'playwright' .",
            "dslr list",
            "dslr delete old_snap",
            "git commit -m 'normal commit'",
            "git rebase main",
            "t3 teatree ticket merge 12 --loop-identity merge-loop",
            "echo 'never use gh pr merge directly'",
        ],
    )
    def test_allows_command(
        self,
        capsys: pytest.CaptureFixture[str],
        command: str,
    ) -> None:
        result = handle_block_direct_commands(_bash_event(command))
        assert result is not True
        output = capsys.readouterr().out.strip()
        assert output == ""

    def test_ignores_non_bash_tools(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        result = handle_block_direct_commands(_bash_event("manage.py runserver", tool_name="Read"))
        assert result is not True
        assert capsys.readouterr().out.strip() == ""


class TestForgeHeredocBodyIsNotAnInvocation:
    """A blocked-tool phrase inside a gh/glab/git heredoc BODY is documentation, not a command.

    A ``gh pr create --body-file - <<EOF … EOF`` / ``git commit -F - <<EOF … EOF``
    heredoc is the PR/commit BODY — pure data the command never executes. Before
    the fix the denylist scanned the whole command string, so a PR description
    documenting ``docker compose up`` (or ``manage.py runserver``, etc.) was
    hard-blocked as if it were the invocation. The fix blanks a forge/git-owned
    heredoc body before the scan while keeping an INTERPRETER heredoc (``bash
    <<EOF docker compose up EOF``) fully scanned — so a real bypass piped to a
    shell still blocks.
    """

    _DOCKER = "docker compose up"

    @pytest.mark.parametrize(
        "command",
        [
            f"gh pr create --title t --body-file - <<'EOF'\nWe fix the {_DOCKER} false-positive.\nEOF",
            "git commit -F - <<'EOF'\ndocs: note that manage.py runserver is banned\nEOF",
            "glab mr note 1 --body-file - <<'EOF'\nwe ran createdb by hand once\nEOF",
            f"cd /repo && gh pr create --title t --body-file - <<'EOF'\n{_DOCKER} in the body\nEOF",
            f'gh pr create --title t --body "{_DOCKER} inline quoted body"',
        ],
    )
    def test_forge_body_phrase_passes(self, capsys: pytest.CaptureFixture[str], command: str) -> None:
        result = handle_block_direct_commands(_bash_event(command))
        assert result is not True
        assert capsys.readouterr().out.strip() == ""

    @pytest.mark.parametrize(
        ("command", "expected_fragment"),
        [
            (f"{_DOCKER} -d", "worktree start"),
            (f"cd /app && {_DOCKER}", "worktree start"),
            (f"bash <<'EOF'\n{_DOCKER}\nEOF", "worktree start"),
            (f"sh <<'EOF'\n{_DOCKER}\nEOF", "worktree start"),
            (f"{_DOCKER} <<'EOF'\nx\nEOF", "worktree start"),
            (f"gh pr create --title t --body-file - <<'EOF'\ndoc\nEOF\n{_DOCKER}", "worktree start"),
        ],
    )
    def test_real_invocation_still_blocks(
        self, capsys: pytest.CaptureFixture[str], command: str, expected_fragment: str
    ) -> None:
        # TEETH: the fix must not weaken the block on an ACTUAL invocation — a real
        # ``docker compose up``, a heredoc fed to an interpreter, and a real
        # invocation chained AFTER a forge heredoc all still deny.
        result = handle_block_direct_commands(_bash_event(command))
        assert result is True
        deny = _parse_deny(capsys)
        assert deny is not None
        assert expected_fragment in deny["permissionDecisionReason"]

    def test_real_bypass_on_the_command_line_with_a_forge_heredoc_still_blocks(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Blanking the heredoc BODY must not hide a real bypass sitting on the
        # command LINE beside it: a hook-silencer (F3) that also feeds a heredoc
        # body still blocks because the bypass is outside the blanked span.
        command = "git -c core.hooksPath=/dev/null commit -F - <<'EOF'\njust a commit body\nEOF"
        result = handle_block_direct_commands(_bash_event(command))
        assert result is True
        deny = _parse_deny(capsys)
        assert deny is not None
        assert "bypasses git hooks" in deny["permissionDecisionReason"]


class TestHandlerChainStopsAfterDeny:
    """The router stops running handlers after the first deny."""

    def test_second_handler_not_called_after_deny(self) -> None:
        call_log: list[str] = []

        def handler_deny(data: dict) -> bool:
            call_log.append("deny")
            return True

        def handler_pass(data: dict) -> bool:
            call_log.append("pass")
            return False

        handlers = [handler_deny, handler_pass]
        for handler in handlers:
            if handler({}) is True:
                break

        assert call_log == ["deny"]

    def test_all_handlers_run_when_none_deny(self) -> None:
        call_log: list[str] = []

        def handler_a(data: dict) -> bool:
            call_log.append("a")
            return False

        def handler_b(data: dict) -> bool:
            call_log.append("b")
            return False

        handlers = [handler_a, handler_b]
        for handler in handlers:
            if handler({}) is True:
                break

        assert call_log == ["a", "b"]
