"""Tests for the per-session TODO-freshness nudge in the hook router.

Mirrors the loop-registration nudge precedent: a UserPromptSubmit handler
that fires at most once per session, is idempotent via a per-session state
file, and never blocks tool use (it prints additionalContext, it does not
emit a deny).
"""

from pathlib import Path

import pytest

import hooks.scripts.hook_router as router
from hooks.scripts.hook_router import handle_todo_freshness_nudge


@pytest.fixture(autouse=True)
def _isolate_state_dir(tmp_path: Path):
    original = router.STATE_DIR
    router.STATE_DIR = tmp_path / "state"
    router.STATE_DIR.mkdir(parents=True, exist_ok=True)
    yield
    router.STATE_DIR = original


class TestTodoFreshnessNudge:
    def test_fires_once_on_first_prompt(self, capsys: pytest.CaptureFixture[str]) -> None:
        result = handle_todo_freshness_nudge({"session_id": "s1"})

        out = capsys.readouterr().out
        assert "task" in out.lower() or "todo" in out.lower()
        # Non-blocking: handlers that emit a deny return True; this one must not.
        assert result is None
        assert (router.STATE_DIR / "s1.todo-nudged").is_file()

    def test_suppressed_on_subsequent_prompts_same_session(self, capsys: pytest.CaptureFixture[str]) -> None:
        handle_todo_freshness_nudge({"session_id": "s1"})
        capsys.readouterr()  # drain the first nudge

        handle_todo_freshness_nudge({"session_id": "s1"})
        handle_todo_freshness_nudge({"session_id": "s1"})

        assert capsys.readouterr().out == ""

    def test_fires_again_for_a_different_session(self, capsys: pytest.CaptureFixture[str]) -> None:
        handle_todo_freshness_nudge({"session_id": "s1"})
        capsys.readouterr()

        handle_todo_freshness_nudge({"session_id": "s2"})

        assert capsys.readouterr().out != ""
        assert (router.STATE_DIR / "s2.todo-nudged").is_file()

    def test_ignores_missing_session_id(self, capsys: pytest.CaptureFixture[str]) -> None:
        result = handle_todo_freshness_nudge({})

        assert capsys.readouterr().out == ""
        assert result is None
        assert not list(router.STATE_DIR.glob("*.todo-nudged"))

    def test_never_blocks_tool_use(self) -> None:
        # A blocking handler returns True (router stops the chain on True).
        # The freshness nudge is advisory only and must never return True.
        assert handle_todo_freshness_nudge({"session_id": "s1"}) is not True
        assert handle_todo_freshness_nudge({"session_id": "s1"}) is not True

    def test_registered_on_user_prompt_submit_event(self) -> None:
        assert handle_todo_freshness_nudge in router._HANDLERS["UserPromptSubmit"]
