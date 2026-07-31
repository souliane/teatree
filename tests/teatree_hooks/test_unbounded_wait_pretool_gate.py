"""PreToolUse gate test: refuse to START an unbounded wait loop (#3882).

``handle_block_unbounded_wait`` denies a Bash ``until``/``while … sleep`` that
carries no deadline and hands back the bounded rewrite. It is a CREATION gate:
nothing here reads process state, and nothing signals a process — the loop that
never gets spawned needs no reaper, and a reaper would have to infer that a
running loop is dead, which this repo has already learned costs live work.
"""

import json
from pathlib import Path

import pytest

import hooks.scripts.hook_router as router
import hooks.scripts.unbounded_wait_guard as guard
from hooks.scripts.hook_router import handle_block_unbounded_wait


@pytest.fixture(autouse=True)
def _isolate_state_dir(tmp_path: Path):
    original = router.STATE_DIR
    router.STATE_DIR = tmp_path / "state"
    router.STATE_DIR.mkdir(parents=True, exist_ok=True)
    yield
    router.STATE_DIR = original


def _bash_event(command: str, tool_name: str = "Bash") -> dict:
    return {
        "session_id": "sess-wait",
        "tool_name": tool_name,
        "tool_input": {"command": command},
    }


def _parse_deny(capsys: pytest.CaptureFixture[str]) -> dict | None:
    output = capsys.readouterr().out.strip()
    if not output:
        return None
    return json.loads(output)


class TestDeniesAnUnboundedWait:
    @pytest.mark.parametrize(
        "command",
        [
            "until gh pr checks 3882 | grep -q pass; do sleep 180; done",
            "until ! pgrep -f 'dev/push-gate.sh' > /dev/null; do sleep 20; done",
            "while true; do gh pr view 1 --json state; sleep 60; done",
            "bash -c 'until test -f /tmp/done; do sleep 30; done'",
        ],
    )
    def test_an_unbounded_wait_is_denied(self, command: str, capsys: pytest.CaptureFixture[str]) -> None:
        assert handle_block_unbounded_wait(_bash_event(command)) is True
        payload = _parse_deny(capsys)
        assert payload is not None
        assert payload["permissionDecision"] == "deny"
        assert "timeout" in payload["permissionDecisionReason"]


class TestAllowsBoundedAndOrdinaryCommands:
    @pytest.mark.parametrize(
        "command",
        [
            "timeout 1800 bash -c 'until test -f /tmp/done; do sleep 30; done'",
            'SECONDS=0; until test -f /tmp/done || [ "$SECONDS" -ge 900 ]; do sleep 20; done',
            "sleep 300",
            "gh pr checks 3882",
            "uv run pytest tests/ -q",
        ],
    )
    def test_a_bounded_or_ordinary_command_passes(self, command: str, capsys: pytest.CaptureFixture[str]) -> None:
        assert handle_block_unbounded_wait(_bash_event(command)) is False
        assert _parse_deny(capsys) is None

    def test_a_non_bash_tool_is_ignored(self, capsys: pytest.CaptureFixture[str]) -> None:
        event = _bash_event("until false; do sleep 5; done", tool_name="Edit")
        assert handle_block_unbounded_wait(event) is False
        assert _parse_deny(capsys) is None


class TestTheGateCanNeverWedgeASession:
    def test_it_is_registered_on_the_pretooluse_chain(self) -> None:
        assert handle_block_unbounded_wait in router._HANDLERS["PreToolUse"]

    def test_an_internal_failure_fails_open(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _raise() -> None:
            raise RuntimeError

        monkeypatch.setattr(guard, "_teatree_src_on_path", _raise)
        assert handle_block_unbounded_wait(_bash_event("until false; do sleep 5; done")) is False
