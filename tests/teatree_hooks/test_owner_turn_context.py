# test-path: cross-cutting
# Runs hooks/scripts/owner_turn_context.py as the PostToolUse hook process it is (no src/teatree mirror).
"""The owner's newest prompt gets its archived-memory recall and any standing rule now due — once, cold, fast.

Nothing runs on ``UserPromptSubmit``, so a prompt never waits on teatree. The first tool call after an
owner prompt carries instead what a prompt hook used to: the cold-tier recall for that prompt (#2746)
and the standing rules whose cadence has passed (#4166), as ONE model-visible
``hookSpecificOutput.additionalContext`` object. Every run here is the real script in its own process,
under the guard that refuses Django, the overlay and any process start.
"""

import fcntl
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hooks.scripts.loop_prompt_shape import LOOP_PROMPT
from teatree import standing_directives_cache
from teatree.loop.standing_directives import SCOPE_ATTENDED, compiled_directives
from tests._cold_hook_guard import COLD_GUARD, COLD_SCRIPT_DRIVER, FAILED_WRITE_DRIVER, FORBIDDEN

_HOOKS = Path(__file__).resolve().parents[2] / "hooks"
_SCRIPT = _HOOKS / "scripts" / "owner_turn_context.py"

_COLD_INDEX = (
    "# Auto Memory — Cold Archive Index\n\n> preamble.\n\n"
    "- feedback_worktree_first.md — always create a worktree before editing project files\n"
)
_RELEVANT = "how do I create a worktree before editing the project files?"
_RECALLED = "feedback_worktree_first.md"
_UNRELATED = "an unrelated question about quantum chromodynamics"
_RULES_HEADER = "Standing rules for this session"
_BUDGET_SECONDS = 0.3
_TIMED_RUNS = 5
_STALL_SECONDS = 30
_CUT_BY_THE_BUDGET = 10 * _BUDGET_SECONDS + 5

#: Runs ``argv[4]`` with the context write (``argv[2] == "write"``) or the cursor write (``"cursor"``)
#: stalled for ``argv[3]`` seconds — a full pipe or a hung state dir — under :data:`COLD_GUARD`.
_STALLED_DRIVER = (
    COLD_GUARD
    + r"""
import runpy, time
sys.path.insert(0, sys.argv[1])
import hooks.scripts.additional_context as additional_context
import hooks.scripts.owner_prompts as owner_prompts
_STALL = float(sys.argv[3])

def _stalled(step):
    def _after_a_stall(*args):
        time.sleep(_STALL)
        return step(*args)
    return _after_a_stall

if sys.argv[2] == "write":
    additional_context.emit_additional_context = _stalled(additional_context.emit_additional_context)
else:
    owner_prompts.UnreadPrompts.mark_read = _stalled(owner_prompts.UnreadPrompts.mark_read)
sys.argv = sys.argv[4:]
runpy.run_path(sys.argv[0], run_name="__main__")
"""
)


def _prompt(text: str, **extra: object) -> dict:
    return {
        "type": "user",
        "origin": {"kind": "human"},
        "timestamp": "2026-10-04T12:00:00Z",
        "message": {"role": "user", "content": text},
        **extra,
    }


def _tool_result() -> dict:
    return {
        "type": "user",
        "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": "ok"}]},
    }


def _assistant() -> dict:
    return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "on it"}]}}


def _append(transcript: Path, *entries: dict) -> None:
    with transcript.open("a", encoding="utf-8") as stream:
        stream.write("".join(json.dumps(entry) + "\n" for entry in entries))


@pytest.fixture
def project(tmp_path: Path) -> Path:
    memory = tmp_path / "project" / "memory"
    memory.mkdir(parents=True)
    (memory / "MEMORY_ARCHIVE.md").write_text(_COLD_INDEX, encoding="utf-8")
    return memory.parent


@pytest.fixture
def transcript(project: Path) -> Path:
    return project / "session.jsonl"


@pytest.fixture
def state(tmp_path: Path) -> Path:
    directory = tmp_path / "state"
    directory.mkdir()
    return directory


@pytest.fixture
def env(state: Path, tmp_path: Path) -> dict[str, str]:
    """An attended, unengaged session that owns the loop slot, never on the SDK lane, its state under the test dir."""
    attended = {
        key: value
        for key, value in os.environ.items()
        if key not in {"CLAUDE_AGENT_SDK_VERSION", "CLAUDE_CODE_ENTRYPOINT"}
    }
    registry = tmp_path / "registry"
    registry.mkdir()
    owner = {"t3-loop-tick-owner": {"session_id": "sess-1", "pid": os.getpid()}}
    (registry / "loop-registry.json").write_text(json.dumps(owner), encoding="utf-8")
    return {
        **attended,
        "T3_HOOK_STATE_DIR": str(state),
        "TEATREE_CLAUDE_STATUSLINE_STATE_DIR": str(state),
        "T3_LOOP_REGISTRY_DIR": str(registry),
        "T3_AUTOLOAD": "0",
    }


@pytest.fixture
def engaged(state: Path) -> None:
    (state / "sess-1.teatree-active").touch()


def _payload(transcript: Path, **extra: object) -> dict:
    return {
        "session_id": "sess-1",
        "transcript_path": str(transcript),
        "cwd": str(transcript.parent),
        "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "ls"},
        **extra,
    }


def _run(
    payload: dict, env: dict[str, str], *, guarded: bool = True, failed_write: str = ""
) -> subprocess.CompletedProcess[str]:
    if failed_write:
        argv = [sys.executable, "-c", FAILED_WRITE_DRIVER, str(_HOOKS.parent), failed_write, str(_SCRIPT)]
    elif guarded:
        argv = [sys.executable, "-c", COLD_SCRIPT_DRIVER, str(_SCRIPT)]
    else:
        argv = [sys.executable, str(_SCRIPT)]
    return subprocess.run(
        argv,
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env=env,
        cwd=payload["cwd"],
    )


def _context(result: subprocess.CompletedProcess[str]) -> str:
    """The model-visible context the run delivered: ONE nested PostToolUse object, or nothing."""
    assert result.returncode == 0, result.stderr
    assert FORBIDDEN not in result.stderr
    if not result.stdout:
        return ""
    document = json.loads(result.stdout)
    assert document == {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": document["hookSpecificOutput"]["additionalContext"],
        }
    }
    return document["hookSpecificOutput"]["additionalContext"]


def _turn_context(transcript: Path, env: dict[str, str], **extra: object) -> str:
    return _context(_run(_payload(transcript, **extra), env))


class TestRecall:
    def test_a_relevant_prompt_gets_its_archived_rule(self, transcript: Path, env: dict[str, str]) -> None:
        _append(transcript, _prompt(_RELEVANT))

        context = _turn_context(transcript, env)

        assert context.startswith("Relevant archived memory rules")
        assert _RECALLED in context

    def test_one_prompt_is_served_once(self, transcript: Path, env: dict[str, str]) -> None:
        _append(transcript, _prompt(_RELEVANT))
        _turn_context(transcript, env)
        _append(transcript, _assistant(), _tool_result())

        assert _turn_context(transcript, env) == ""

    def test_a_new_prompt_is_served_again(self, transcript: Path, env: dict[str, str]) -> None:
        _append(transcript, _prompt(_RELEVANT))
        _turn_context(transcript, env)
        _append(
            transcript, _assistant(), _tool_result(), _prompt("and remind me: worktree before editing project files?")
        )

        assert _RECALLED in _turn_context(transcript, env)

    def test_a_slash_command_prompt_gets_its_archived_rule(self, transcript: Path, env: dict[str, str]) -> None:
        typed = (
            "<command-message>t3:code is running…</command-message>\n"
            f"<command-name>/t3:code</command-name>\n<command-args>{_RELEVANT}</command-args>"
        )
        _append(transcript, _prompt(typed))

        assert _RECALLED in _turn_context(transcript, env)

    def test_an_unrelated_prompt_gets_nothing(self, transcript: Path, env: dict[str, str]) -> None:
        _append(transcript, _prompt(_UNRELATED))

        assert _turn_context(transcript, env) == ""

    def test_no_memory_dir_gets_nothing(self, transcript: Path, env: dict[str, str], project: Path) -> None:
        (project / "memory" / "MEMORY_ARCHIVE.md").unlink()
        _append(transcript, _prompt(_RELEVANT))

        assert _turn_context(transcript, env) == ""

    def test_the_project_dir_named_after_the_cwd_is_the_fallback(self, tmp_path: Path, env: dict[str, str]) -> None:
        workspace = tmp_path / "workspace" / "proj"
        workspace.mkdir(parents=True)
        memory = Path(env["HOME"]) / ".claude" / "projects" / str(workspace).replace("/", "-") / "memory"
        memory.mkdir(parents=True)
        (memory / "MEMORY_ARCHIVE.md").write_text(_COLD_INDEX, encoding="utf-8")
        transcript = workspace / "elsewhere.jsonl"
        _append(transcript, _prompt(_RELEVANT))

        assert _RECALLED in _turn_context(transcript, env, cwd=str(workspace))


class TestOneCallAtATime:
    def test_a_call_while_another_holds_the_session_stays_silent_and_leaves_the_prompt(
        self, transcript: Path, env: dict[str, str], state: Path
    ) -> None:
        _append(transcript, _prompt(_RELEVANT))

        with (state / "sess-1.owner-turn-cursor").open("a") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            assert _turn_context(transcript, env) == ""

        assert _RECALLED in _turn_context(transcript, env)


class TestWhatIsNotTheOwner:
    @pytest.mark.parametrize(
        "entry",
        [
            _prompt(_RELEVANT, turnOrigin="scheduled", scheduledFireId="f1"),
            _prompt(LOOP_PROMPT),
            _prompt(_RELEVANT, isMeta=True),
        ],
        ids=["scheduled-fire", "loop-tick", "meta"],
    )
    def test_a_machine_turn_gets_nothing(
        self, transcript: Path, env: dict[str, str], engaged: None, entry: dict
    ) -> None:
        _append(transcript, entry)

        assert _turn_context(transcript, env) == ""

    def test_a_sub_agent_call_gets_nothing_and_leaves_the_prompt_for_the_main_agent(
        self, transcript: Path, env: dict[str, str]
    ) -> None:
        _append(transcript, _prompt(_RELEVANT))

        assert _turn_context(transcript, env, agent_id="a4ad83956ff699aaa", agent_type="Explore") == ""
        assert _RECALLED in _turn_context(transcript, env)


class TestStandingRules:
    def test_an_engaged_session_gets_its_due_rules_in_the_same_single_object(
        self, transcript: Path, env: dict[str, str], engaged: None
    ) -> None:
        _append(transcript, _prompt(_RELEVANT))

        context = _turn_context(transcript, env)

        assert _RECALLED in context
        assert f"{_RULES_HEADER} ({len(compiled_directives())})" in context
        assert all(directive.text in context for directive in compiled_directives())

    def test_rules_inside_their_interval_are_not_repeated_on_the_next_prompt(
        self, transcript: Path, env: dict[str, str], engaged: None
    ) -> None:
        _append(transcript, _prompt(_UNRELATED))
        assert _RULES_HEADER in _turn_context(transcript, env)
        _append(transcript, _assistant(), _prompt(_UNRELATED + " again"))

        assert _turn_context(transcript, env) == ""

    def test_the_published_rules_are_what_is_delivered(
        self, transcript: Path, env: dict[str, str], engaged: None
    ) -> None:
        standing_directives_cache.write(
            [{"slot_id": "slot-a", "cadence_seconds": 120, "text": "Alpha rule.", "scope": SCOPE_ATTENDED}]
        )
        _append(transcript, _prompt(_UNRELATED))

        assert (
            _turn_context(transcript, env)
            == f"{_RULES_HEADER} (1) — they apply to your next reply:\n  - [slot-a] Alpha rule."
        )

    def test_an_unengaged_session_gets_no_rules(self, transcript: Path, env: dict[str, str]) -> None:
        _append(transcript, _prompt(_RELEVANT))

        assert _RULES_HEADER not in _turn_context(transcript, env)

    def test_the_autoload_read_stays_cold(self, transcript: Path, env: dict[str, str]) -> None:
        del env["T3_AUTOLOAD"]
        _append(transcript, _prompt(_UNRELATED))

        assert _turn_context(transcript, env) == ""


class TestTheRecordFollowsTheWrite:
    """The prompt and the rules are recorded as delivered only once the context was written."""

    @pytest.mark.parametrize("failure", ["overrun", "unread-pipe"])
    def test_a_failed_write_records_nothing_and_the_next_call_delivers(
        self, transcript: Path, env: dict[str, str], engaged: None, failure: str
    ) -> None:
        _append(transcript, _prompt(_RELEVANT))

        failed = _run(_payload(transcript), env, failed_write=failure)

        assert (failed.returncode, failed.stdout) == (0, ""), failed.stderr
        context = _turn_context(transcript, env)
        assert _RECALLED in context
        assert _RULES_HEADER in context

    def test_a_non_owner_session_never_gets_the_host_wide_slot(
        self, transcript: Path, env: dict[str, str], state: Path
    ) -> None:
        (state / "sess-2.teatree-active").touch()
        board = next(d.text for d in compiled_directives() if d.slot_id == "standing-pr-board")
        _append(transcript, _prompt(_UNRELATED))

        context = _context(_run(_payload(transcript, session_id="sess-2"), env))

        assert _RULES_HEADER in context
        assert board not in context


class TestTheBudget:
    def test_a_long_session_is_served_well_inside_the_budget(self, transcript: Path, env: dict[str, str]) -> None:
        history = json.dumps(_assistant()) + "\n" + json.dumps(_tool_result()) + "\n"
        transcript.write_text(history * 20_000, encoding="utf-8")
        _append(transcript, _prompt(_RELEVANT), *([_assistant(), _tool_result()] * 200))
        cursor = Path(env["T3_HOOK_STATE_DIR"]) / "sess-1.owner-turn-cursor"
        samples = []
        for _ in range(_TIMED_RUNS):
            cursor.unlink(missing_ok=True)
            started = time.perf_counter()
            result = _run(_payload(transcript), env, guarded=False)
            samples.append(time.perf_counter() - started)
            assert _RECALLED in _context(result)

        assert min(samples) < _BUDGET_SECONDS, samples

    def test_an_overrun_exits_silently(self, tmp_path: Path, env: dict[str, str]) -> None:
        stalled = tmp_path / "stalled.jsonl"
        os.mkfifo(stalled)
        started = time.perf_counter()

        result = _run(_payload(stalled), env, guarded=False)

        assert (result.returncode, result.stdout) == (0, "")
        assert time.perf_counter() - started < _CUT_BY_THE_BUDGET

    @pytest.mark.parametrize("stalled", ["write", "cursor"])
    def test_the_budget_covers_the_write_and_the_cursor(
        self, transcript: Path, env: dict[str, str], stalled: str
    ) -> None:
        _append(transcript, _prompt(_RELEVANT))
        started = time.perf_counter()

        result = subprocess.run(
            [sys.executable, "-c", _STALLED_DRIVER, str(_HOOKS.parent), stalled, str(_STALL_SECONDS), str(_SCRIPT)],
            input=json.dumps(_payload(transcript)),
            capture_output=True,
            text=True,
            timeout=2 * _STALL_SECONDS,
            check=False,
            env=env,
            cwd=transcript.parent,
        )

        assert time.perf_counter() - started < _CUT_BY_THE_BUDGET
        assert result.returncode == 0, result.stderr
        assert _RECALLED in _turn_context(transcript, env)

    def test_a_range_far_past_what_the_budget_can_parse_still_delivers_the_newest_prompt(
        self, transcript: Path, env: dict[str, str]
    ) -> None:
        _append(transcript, _prompt(_UNRELATED))
        _turn_context(transcript, env)
        with transcript.open("a", encoding="utf-8") as stream:
            stream.write('{"type":"progress"}\n' * 2_000_000)
        _append(transcript, _prompt(_RELEVANT))

        assert _RECALLED in _turn_context(transcript, env)

    def test_a_range_far_past_what_the_budget_can_parse_still_moves_the_cursor(
        self, transcript: Path, env: dict[str, str]
    ) -> None:
        _append(transcript, _prompt(_UNRELATED))
        _turn_context(transcript, env)
        with transcript.open("a", encoding="utf-8") as stream:
            stream.write('{"type":"progress"}\n' * 2_000_000)
        assert _turn_context(transcript, env) == ""
        cursor = Path(env["T3_HOOK_STATE_DIR"]) / "sess-1.owner-turn-cursor"
        assert cursor.read_text(encoding="utf-8") == str(transcript.stat().st_size)
        _append(transcript, _prompt(_RELEVANT))

        assert _RECALLED in _turn_context(transcript, env)


class TestRegistration:
    def test_it_runs_after_every_tool_call_in_its_own_bounded_process(self) -> None:
        groups = json.loads((_HOOKS / "hooks.json").read_text(encoding="utf-8"))["hooks"]["PostToolUse"]

        assert {
            "matcher": "*",
            "hooks": [
                {
                    "type": "command",
                    "command": "${CLAUDE_PLUGIN_ROOT}/hooks/scripts/run-hook.sh "
                    "${CLAUDE_PLUGIN_ROOT}/hooks/scripts/owner_turn_context.py",
                    "timeout": 5,
                }
            ],
        } in groups
