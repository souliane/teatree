"""PreToolUse gate test: deny a raw, guessed-pid ``kill`` (#2225).

The handler ``handle_block_raw_pid_kill`` routes a Bash ``kill <pid>`` /
``kill -9 <pid>`` through the safe-kill helper guidance instead of letting the
agent signal a process it identified by 'looks idle'. ``pkill``/``killall`` and
``%job``/``$VAR`` targets pass through.
"""

import json
from pathlib import Path

import pytest

import hooks.scripts.hook_router as router
from hooks.scripts.hook_router import handle_block_raw_pid_kill


@pytest.fixture(autouse=True)
def _isolate_state_dir(tmp_path: Path):
    original = router.STATE_DIR
    router.STATE_DIR = tmp_path / "state"
    router.STATE_DIR.mkdir(parents=True, exist_ok=True)
    yield
    router.STATE_DIR = original


def _bash_event(command: str, tool_name: str = "Bash") -> dict:
    return {
        "session_id": "sess-kill",
        "tool_name": tool_name,
        "tool_input": {"command": command},
    }


def _parse_deny(capsys: pytest.CaptureFixture[str]) -> dict | None:
    output = capsys.readouterr().out.strip()
    if not output:
        return None
    return json.loads(output)


class TestDeniesRawPidKill:
    @pytest.mark.parametrize(
        "command",
        ["kill 4242", "kill -9 4242", "kill -SIGKILL 4242", "kill -s TERM 4242"],
    )
    def test_raw_pid_kill_is_denied(self, command: str, capsys: pytest.CaptureFixture[str]) -> None:
        assert handle_block_raw_pid_kill(_bash_event(command)) is True
        payload = _parse_deny(capsys)
        assert payload is not None
        assert payload["permissionDecision"] == "deny"
        assert "t3 teatree safe-kill" in payload["permissionDecisionReason"]


class TestAllowsSafeCommands:
    @pytest.mark.parametrize(
        "command",
        [
            "kill -0 4242",
            "pkill -9 chrome",
            "killall node",
            "kill %1",
            "kill $PID",
            "kill -9 $(pgrep claude)",
            "ps -axo pid,comm",
        ],
    )
    def test_non_raw_pid_kill_passes(self, command: str, capsys: pytest.CaptureFixture[str]) -> None:
        assert handle_block_raw_pid_kill(_bash_event(command)) is False
        assert _parse_deny(capsys) is None

    def test_non_bash_tool_passes(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert handle_block_raw_pid_kill(_bash_event("kill 4242", tool_name="Edit")) is False
        assert _parse_deny(capsys) is None
