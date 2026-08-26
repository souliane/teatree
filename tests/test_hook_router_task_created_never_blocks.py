"""The router must NEVER exit non-zero on ``TaskCreated`` — a deny DESTROYS data.

Unlike every other blockable event, a ``TaskCreated`` deny does not *prevent*
anything. The harness ``TaskCreate`` tool persists the task FIRST, then runs the
hooks, and on a blocking result DELETES the task it just wrote before raising.
So the block is applied to a row that already exists — the user's work item is
gone, and the message they see is the exit-2 fallback ``No stderr output``
(teatree keeps stderr clean on deny, and the ``{"continue": false}`` envelope
maps to ``preventContinuation``/``stopReason``, never to the ``blockingError``
the tool actually reads).

The invariant pinned here is therefore structural, not per-handler: whatever is
registered on ``TaskCreated``, and whatever it returns, ``main()`` exits 0. A
gate that wants to speak on this event has ``{"systemMessage": …}`` at exit 0;
it does not get to block.

Integration-style: ``main()`` runs as a subprocess so the real exit code
propagates, plus an in-process forced-deny probe that is independent of which
handlers happen to be registered today.
"""

import json
import os
import subprocess
import sys
import tempfile
from io import StringIO
from pathlib import Path

import pytest

import hooks.scripts.hook_router as router

HOOK_ROUTER = Path(__file__).resolve().parent.parent / "hooks" / "scripts" / "hook_router.py"


def _seed_skill(skills_dir: Path, name: str) -> None:
    skill = skills_dir / name
    skill.mkdir(parents=True, exist_ok=True)
    frontmatter = ["---", f"name: {name}", 'description: "Fixture skill for the TaskCreated exit-code test."', "---"]
    (skill / "SKILL.md").write_text("\n".join([*frontmatter, f"# {name}", ""]), encoding="utf-8")


@pytest.fixture
def demanding_session(tmp_path: Path) -> dict[str, str]:
    """A session whose ``<session>.pending`` carries a resolvable, unreferenced demand.

    This is the live shape that destroyed nine real work items: the parent has a
    pending skill demand, and a task-list item — which spawns nothing and has no
    dispatch prompt — can never reference it.
    """
    state = tmp_path / "state"
    state.mkdir()
    (state / "sess-todo.pending").write_text("review\n", encoding="utf-8")
    (state / "sess-todo.skills").write_text("review\n", encoding="utf-8")

    skills_dir = tmp_path / "skills"
    _seed_skill(skills_dir, "review")

    home = tmp_path / "home"
    home.mkdir(exist_ok=True)  # the suite's HOME-isolation fixture may already own it
    return {
        "HOME": str(home),
        "USERPROFILE": str(home),
        "T3_HOOK_STATE_DIR": str(state),
        "T3_SKILL_SEARCH_DIRS": str(skills_dir),
    }


def _run_router(event: str, payload: dict, env_extra: dict[str, str]) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, **env_extra}
    env.pop("XDG_DATA_HOME", None)
    env.pop("T3_CONFIG_DB", None)
    return subprocess.run(
        [sys.executable, str(HOOK_ROUTER), "--event", event],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
        env=env,
    )


def _todo_item(session_id: str = "sess-todo") -> dict:
    return {
        "session_id": session_id,
        "hook_event_name": "TaskCreated",
        "task_id": "task-1",
        "task_subject": "Rework the acceptance-rule fixtures",
        "task_description": "The fixtures drifted from the live schema; realign them.",
    }


class TestTaskCreatedExitsZero:
    """A todo item survives task creation whatever the session's pending demand."""

    def test_unreferenced_pending_demand_does_not_block_task_creation(self, demanding_session: dict[str, str]) -> None:
        result = _run_router("TaskCreated", _todo_item(), demanding_session)
        assert result.returncode == 0, (
            f"a TaskCreated deny DELETES the task (got rc={result.returncode}); stdout={result.stdout!r}"
        )

    def test_no_blocking_envelope_reaches_stdout(self, demanding_session: dict[str, str]) -> None:
        # ``{"continue": false}`` maps to preventContinuation, and ``decision:
        # block`` maps to blockingError — which deletes the task. Neither may be
        # emitted on this event.
        result = _run_router("TaskCreated", _todo_item(), demanding_session)
        raw = result.stdout.strip()
        if not raw:
            return
        payload = json.loads(raw)
        assert payload.get("continue") is not False
        assert payload.get("decision") != "block"


class TestRegisteredDenyCannotBlockTaskCreated:
    """A handler that DOES deny still cannot make the router exit non-zero.

    The structural half of the invariant, and the half that outlives any particular
    arm: retiring the handlers that denied leaves the NEXT one free to, so the
    refusal is declared on the event rather than argued per handler.
    """

    def test_task_created_is_declared_never_blocking(self) -> None:
        assert "TaskCreated" in router._NEVER_BLOCKING_EVENTS

    def test_forced_deny_still_exits_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(router._HANDLERS, "TaskCreated", [lambda _data: True])
        monkeypatch.setattr(sys, "argv", ["hook_router.py", "--event", "TaskCreated"])
        monkeypatch.setattr("sys.stdin", StringIO(json.dumps(_todo_item())))

        # No SystemExit at all is the contract: main() returns normally.
        assert router.main() is None

    def test_pretooluse_still_exits_two_on_deny(self) -> None:
        # The never-blocking carve-out must not leak onto the events whose deny
        # IS honoured at exit 2 and IS non-destructive.
        assert "PreToolUse" not in router._NEVER_BLOCKING_EVENTS
        with tempfile.TemporaryDirectory() as home:
            result = _run_router(
                "PreToolUse",
                {"tool_name": "Bash", "tool_input": {"command": "uv run pytest --no-cov -q"}},
                {"HOME": home, "USERPROFILE": home},
            )
        assert result.returncode == 2
