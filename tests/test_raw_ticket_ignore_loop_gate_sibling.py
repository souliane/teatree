"""The raw-ticket-ignore-loop gate: extraction contract + behaviour (#2663 dream-batch dea750a552f8f2d2).

``teatree.core.gates.bulk_close_gate`` names its own residual: an agent invoking the
single-item ``t3 <overlay> ticket transition <id> ignore`` command directly, N times
in a row, is "the CLI's own boundary, not a hole this gate can close on its own."
``hooks/scripts/raw_ticket_ignore_loop_gate.py`` is that closing — these tests pin
both the extraction contract (single canonical identity, router re-export, cold
import — the same shape ``test_raw_pid_kill_guard_sibling.py`` pins) and the
threshold behaviour itself.
"""

import subprocess
import sys
from pathlib import Path

import pytest

import hooks.scripts.hook_router as router
import hooks.scripts.raw_ticket_ignore_loop_gate as gate

_SCRIPTS_DIR = Path(router.__file__).resolve().parent

_IGNORE_COMMAND = "t3 teatree ticket transition 42 ignore"


class TestCanonicalIdentity:
    def test_module_has_one_canonical_package_identity(self) -> None:
        assert sys.modules["hooks.scripts.raw_ticket_ignore_loop_gate"] is gate


class TestRouterReExportReachable:
    def test_reexport_is_the_same_object(self) -> None:
        assert router.handle_block_raw_ticket_ignore_loop is gate.handle_block_raw_ticket_ignore_loop

    def test_registered_in_pretooluse_chain(self) -> None:
        assert gate.handle_block_raw_ticket_ignore_loop in router._HANDLERS["PreToolUse"]


class TestRawTicketIgnoreMatch:
    @pytest.mark.parametrize(
        "command",
        [
            "t3 teatree ticket transition 42 ignore",
            "t3 souliane-prod ticket transition 1234 ignore",
            "  t3 teatree ticket transition 7 ignore  ",
        ],
    )
    def test_matches_the_raw_ignore_shape(self, command: str) -> None:
        assert gate.raw_ticket_ignore_match(command) is True

    @pytest.mark.parametrize(
        "command",
        [
            "t3 teatree ticket bulk-close 1 2 3",
            "t3 teatree ticket transition 42 unignore",
            "t3 teatree ticket transition 42 code",
            "echo 't3 teatree ticket transition 42 ignore'",
            "",
        ],
    )
    def test_does_not_match_other_commands(self, command: str) -> None:
        assert gate.raw_ticket_ignore_match(command) is False


class TestThreshold:
    """1-2 calls stay allowed; the 3rd (and every further) call is denied."""

    def _event(self, session_id: str = "sess-1") -> dict:
        return {"session_id": session_id, "tool_name": "Bash", "tool_input": {"command": _IGNORE_COMMAND}}

    def test_a_single_call_is_allowed(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(router, "STATE_DIR", tmp_path)
        assert gate.handle_block_raw_ticket_ignore_loop(self._event()) is False

    def test_a_second_call_is_still_allowed(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(router, "STATE_DIR", tmp_path)
        event = self._event()
        assert gate.handle_block_raw_ticket_ignore_loop(event) is False
        assert gate.handle_block_raw_ticket_ignore_loop(event) is False

    def test_the_third_call_is_denied(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        monkeypatch.setattr(router, "STATE_DIR", tmp_path)
        event = self._event()
        assert gate.handle_block_raw_ticket_ignore_loop(event) is False
        assert gate.handle_block_raw_ticket_ignore_loop(event) is False
        assert gate.handle_block_raw_ticket_ignore_loop(event) is True
        out = capsys.readouterr().out
        assert "bulk-close" in out

    def test_a_fourth_call_stays_denied_too(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(router, "STATE_DIR", tmp_path)
        event = self._event()
        for _ in range(3):
            gate.handle_block_raw_ticket_ignore_loop(event)
        assert gate.handle_block_raw_ticket_ignore_loop(event) is True

    def test_two_sessions_count_independently(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(router, "STATE_DIR", tmp_path)
        a, b = self._event("sess-a"), self._event("sess-b")
        gate.handle_block_raw_ticket_ignore_loop(a)
        gate.handle_block_raw_ticket_ignore_loop(a)
        # session b's own count starts fresh — its 2nd call is still allowed.
        gate.handle_block_raw_ticket_ignore_loop(b)
        assert gate.handle_block_raw_ticket_ignore_loop(b) is False

    def test_a_non_matching_command_is_never_counted(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(router, "STATE_DIR", tmp_path)
        other = {
            "session_id": "sess-1",
            "tool_name": "Bash",
            "tool_input": {"command": "t3 teatree ticket bulk-close 1"},
        }
        for _ in range(5):
            assert gate.handle_block_raw_ticket_ignore_loop(other) is False

    def test_a_non_bash_tool_is_ignored(self) -> None:
        event = {"session_id": "sess-1", "tool_name": "Edit", "tool_input": {"command": _IGNORE_COMMAND}}
        assert gate.handle_block_raw_ticket_ignore_loop(event) is False

    def test_no_session_id_fails_open(self) -> None:
        event = {"session_id": "", "tool_name": "Bash", "tool_input": {"command": _IGNORE_COMMAND}}
        assert gate.handle_block_raw_ticket_ignore_loop(event) is False


class TestColdImport:
    def test_imports_with_stdlib_only_no_django(self) -> None:
        """A fresh interpreter imports the sibling without Django configured or teatree loaded."""
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import sys; sys.path.insert(0, sys.argv[1]); "
                    "import raw_ticket_ignore_loop_gate as s; "
                    "assert 'django' not in sys.modules, 'django imported at module top'; "
                    "assert not any(m == 'teatree' or m.startswith('teatree.') for m in sys.modules), "
                    "'teatree imported at module top'; "
                    "print(s.handle_block_raw_ticket_ignore_loop({'tool_name': 'Edit', 'tool_input': {}}))"
                ),
                str(_SCRIPTS_DIR),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
            env={"PATH": "/usr/bin:/bin"},
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "False"
