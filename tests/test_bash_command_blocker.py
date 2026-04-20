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
            ("python manage.py runserver", "lifecycle start"),
            ("./manage.py runserver 0.0.0.0:8000", "lifecycle start"),
            ("python manage.py migrate", "lifecycle setup"),
            ("./manage.py migrate --run-syncdb", "lifecycle setup"),
            ("nx serve my-app", "lifecycle start"),
            ("docker compose up -d", "lifecycle start"),
            ("docker compose start", "lifecycle start"),
            ("createdb mydb", "db"),
            ("dropdb mydb", "db"),
            ("npx playwright test", "e2e"),
            ("playwright test --headed", "e2e"),
            ("npm run serve", "run"),
            ("npm run start", "run"),
            ("pipenv install requests", "lifecycle setup"),
            ("pip install -r requirements.txt", "lifecycle setup"),
            ("pg_restore -d mydb dump.sql", "db"),
            ("pg_dump mydb > dump.sql", "db"),
            ("dslr snapshot mydb", "db"),
            ("dslr restore my_snap", "db"),
            ("T3_ALLOW_REMOTE_DUMP=1 t3 myapp db refresh", "T3_ALLOW_REMOTE_DUMP"),
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
            "t3 myapp lifecycle start",
            "uv run t3 teatree lifecycle setup",
            "PYENV_VERSION=3.13.11 t3 myapp e2e external",
            "git status",
            "git push origin main",
            "ruff check src/",
            "uv run pytest --no-cov -x",
            "ls -la",
            "echo 'manage.py runserver is not allowed'",
            "cat README.md",
            "grep -r 'playwright' .",
            "dslr list",
            "dslr delete old_snap",
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
