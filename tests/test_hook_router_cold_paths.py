# test-path: cross-cutting
# Drives hooks/scripts/hook_router.py main() and reads both harness manifests; no src/teatree mirror.
"""No prompt hook, and the prompt-time work's new homes never boot Django, the overlay or a process.

A hook subprocess that imports Django pays for every overlay package too, which can outlast Claude
Code's 30 s hook timeout on a loaded host. A session opened before the manifest change keeps its
registration until restart, so the router's prompt path is itself a no-op.
"""

import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

import hooks.scripts.hook_router as router
from hooks.scripts.stop_snapshot_slot import _MARKER_PREFIX
from teatree import answer_handback
from tests._cold_hook_guard import COLD_GUARD, FORBIDDEN
from tests._unreadable_file import skip_if_root

_PLUGIN_ROOT = Path(router.__file__).resolve().parents[2]
_MANIFESTS = ("hooks.json", "codex.json")
_HANDLER = "HANDLER"

# Runs the real router main() under the cold guard, with every handler recorded.
_DRIVER = (
    COLD_GUARD
    + r"""
sys.path.insert(0, sys.argv[1])
import hooks.scripts.hook_router as router

def _recorded(handler):
    def run(data):
        sys.stderr.write(f"HANDLER {handler.__name__}\n")
        return handler(data)
    return run

for chain in router._HANDLERS.values():
    chain[:] = [_recorded(handler) for handler in chain]
if sys.argv[3:] == ["unread-pipe"]:
    _unread, _written = os.pipe()
    os.close(_unread)
    os.dup2(_written, sys.stdout.fileno())
sys.argv = ["hook_router.py", "--event", sys.argv[2]]
router.main()
"""
)


def _drive(event: str, payload: dict, env: dict[str, str], *stdout: str) -> subprocess.CompletedProcess[str]:
    """Run the router for *event*; ``"unread-pipe"`` makes its stdout a pipe whose reader is gone."""
    return subprocess.run(
        [sys.executable, "-c", _DRIVER, str(_PLUGIN_ROOT), event, *stdout],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env={**os.environ, **env},
    )


def _snapshot_slot_published_just_now(env: dict[str, str], session: str) -> None:
    """The snapshot slot's five-minutely publish is the one Stop leg that reads the DB."""
    stamp = Path(env["T3_HOOK_STATE_DIR"]) / f"{_MARKER_PREFIX}{session}.stamp"
    stamp.write_text(str(time.time()), encoding="utf-8")


def _handlers_run(stderr: str) -> list[str]:
    return [line.split()[1] for line in stderr.splitlines() if line.startswith(_HANDLER)]


@pytest.fixture
def transcript(tmp_path: Path) -> Path:
    path = tmp_path / "transcript.jsonl"
    entry = {"type": "user", "origin": {"kind": "human"}, "timestamp": "2026-09-29T12:00:00Z"}
    path.write_text(json.dumps({**entry, "message": {"role": "user", "content": "status?"}}) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def attended(tmp_path: Path) -> dict[str, str]:
    """An interactive session while another live session owns the loop: the owner's everyday case."""
    state, registry = tmp_path / "state", tmp_path / "registry"
    state.mkdir()
    registry.mkdir()
    owner = {router._OWNER_LOOP: {"session_id": "the-loop-owner", "pid": os.getpid()}}
    (registry / "loop-registry.json").write_text(json.dumps(owner), encoding="utf-8")
    return {
        "T3_HOOK_STATE_DIR": str(state),
        "TEATREE_CLAUDE_STATUSLINE_STATE_DIR": str(state),
        "T3_LOOP_REGISTRY_DIR": str(registry),
        "XDG_DATA_HOME": str(tmp_path / "data"),
    }


@pytest.mark.parametrize("manifest", _MANIFESTS)
def test_no_harness_manifest_registers_a_prompt_hook(manifest: str) -> None:
    hooks = json.loads((_PLUGIN_ROOT / "hooks" / manifest).read_text(encoding="utf-8"))["hooks"]

    assert "UserPromptSubmit" not in hooks


def test_the_router_carries_no_prompt_handler() -> None:
    assert "UserPromptSubmit" not in router._HANDLERS


def test_a_prompt_in_a_session_registered_before_the_change_runs_nothing(
    transcript: Path, attended: dict[str, str]
) -> None:
    session = "cold-ups"
    for marker in ("teatree-active", "t3-engaged"):
        (Path(attended["T3_HOOK_STATE_DIR"]) / f"{session}.{marker}").touch()
    payload = {"session_id": session, "transcript_path": str(transcript), "prompt": "fix the bug in foo.py"}

    result = _drive("UserPromptSubmit", payload, {**attended, "T3_AUTOLOAD": "1"})

    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")


def test_the_whole_stop_chain_runs_cold_and_rearms_the_turn_state(transcript: Path, attended: dict[str, str]) -> None:
    session = "cold-stop"
    state = Path(attended["T3_HOOK_STATE_DIR"])
    _snapshot_slot_published_just_now(attended, session)
    (state / f"{session}.classifier-deny").write_text(json.dumps({"action": "Bash: rm"}), encoding="utf-8")
    counters = [state / f"{session}.{suffix}" for suffix in ("turn-tool-count", "turn-budget-nudged")]
    for counter in counters:
        counter.write_text("30", encoding="utf-8")
    payload = {"session_id": session, "transcript_path": str(transcript)}

    nagged = _drive("Stop", payload, attended)
    quiet = _drive("Stop", payload, attended)

    assert "Classifier denied Bash: rm" in nagged.stdout
    assert "Classifier denied" not in quiet.stdout
    assert _handlers_run(quiet.stderr) == [handler.__name__ for handler in router._HANDLERS["Stop"]]
    assert not any(counter.exists() for counter in counters)
    assert (nagged.returncode, quiet.returncode) == (0, 0)
    assert FORBIDDEN not in nagged.stderr + quiet.stderr


def test_a_slack_answer_is_handed_back_to_the_asking_session_once_and_cold(
    transcript: Path, attended: dict[str, str]
) -> None:
    session = "cold-handback"
    _snapshot_slot_published_just_now(attended, session)
    answer_handback.post(session_id=session, question_id=7, answer="use postgres-1", env=attended)
    payload = {"session_id": session, "transcript_path": str(transcript)}

    delivered = _drive("Stop", payload, attended)
    again = _drive("Stop", payload, attended)

    decision = json.loads(delivered.stdout)
    assert decision["decision"] == "block"
    assert "#7" in decision["reason"]
    assert "use postgres-1" in decision["reason"]
    assert "use postgres-1" not in again.stdout
    assert FORBIDDEN not in delivered.stderr + again.stderr


@skip_if_root
def test_a_mailbox_the_hook_cannot_claim_from_says_so_on_stderr(transcript: Path, attended: dict[str, str]) -> None:
    session = "cold-locked"
    _snapshot_slot_published_just_now(attended, session)
    answer_handback.post(session_id=session, question_id=7, answer="use postgres-1", env=attended)
    box = answer_handback.mailbox(session, env=attended)
    assert box is not None
    box.chmod(0o500)
    try:
        result = _drive("Stop", {"session_id": session, "transcript_path": str(transcript)}, attended)
    finally:
        box.chmod(0o755)

    assert "use postgres-1" not in result.stdout
    assert "Could not claim the handed-back answer" in result.stderr


def test_a_waiting_answer_never_switches_off_a_turn_end_gate(transcript: Path, attended: dict[str, str]) -> None:
    session = "cold-gated"
    state = Path(attended["T3_HOOK_STATE_DIR"])
    _snapshot_slot_published_just_now(attended, session)
    (state / f"{session}.classifier-deny").write_text(json.dumps({"action": "Bash: rm"}), encoding="utf-8")
    answer_handback.post(session_id=session, question_id=7, answer="use postgres-1", env=attended)
    payload = {"session_id": session, "transcript_path": str(transcript)}

    gated = _drive("Stop", payload, attended)
    delivered = _drive("Stop", {**payload, "stop_hook_active": True}, attended)

    assert "Classifier denied Bash: rm" in gated.stdout
    assert "use postgres-1" not in gated.stdout
    assert "use postgres-1" in json.loads(delivered.stdout)["reason"]


def test_a_resuming_session_gets_its_answer_with_django_refused_and_no_process_started(
    attended: dict[str, str],
) -> None:
    session = "cold-resume"
    answer_handback.post(session_id=session, question_id=7, answer="use postgres-1", env=attended)

    result = _drive("SessionStart", {"session_id": session, "source": "resume"}, attended)

    context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
    assert 'Your AskUserQuestion #7 was answered by the user: "use postgres-1". Apply it now.' in context
    # SessionStart's hand-off claim reads the DB by design; only a process start is refused outright.
    assert [line for line in result.stderr.splitlines() if line.startswith(f"{FORBIDDEN} os.")] == []
    assert [line for line in result.stderr.splitlines() if line.startswith(f"{FORBIDDEN} subprocess")] == []


def test_a_stop_whose_write_never_reaches_the_session_hands_the_answer_back(
    transcript: Path, attended: dict[str, str]
) -> None:
    session = "cold-unread-stop"
    _snapshot_slot_published_just_now(attended, session)
    answer_handback.post(session_id=session, question_id=7, answer="use postgres-1", env=attended)
    payload = {"session_id": session, "transcript_path": str(transcript)}

    lost = _drive("Stop", payload, attended, "unread-pipe")
    delivered = _drive("Stop", payload, attended)

    assert lost.returncode == 0, lost.stderr
    assert "use postgres-1" in json.loads(delivered.stdout)["reason"]


def test_a_start_whose_write_never_reaches_the_session_hands_back_the_answer_and_the_mirror(
    attended: dict[str, str],
) -> None:
    session = "cold-unread-start"
    answer_handback.post(session_id=session, question_id=7, answer="use postgres-1", env=attended)
    mirror = Path(attended["XDG_DATA_HOME"]) / "teatree" / "handover" / "latest.md"
    mirror.parent.mkdir(parents=True)
    mirror.write_text("THE PARKED HAND-OFF", encoding="utf-8")
    payload = {"session_id": session, "source": "resume"}

    lost = _drive("SessionStart", payload, attended, "unread-pipe")
    delivered = _drive("SessionStart", payload, attended)

    assert lost.returncode == 0, lost.stderr
    context = json.loads(delivered.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "use postgres-1" in context
    assert "THE PARKED HAND-OFF" in context


def test_the_hand_back_is_the_last_stop_decision_before_the_loop_pump() -> None:
    assert [handler.__name__ for handler in router._HANDLERS["Stop"][-2:]] == [
        "handle_hand_back_answers",
        "handle_loop_self_pump",
    ]


# Edit/Write (the plan gate's ticket lookup), Agent (the admission governor) and AskUserQuestion
# (the Slack mirror) read the DB by design; no other tool may.
@pytest.mark.parametrize(
    ("event", "tool_name", "tool_input"),
    [
        ("PreToolUse", "Read", {"file_path": "/tmp/x"}),
        ("PreToolUse", "Grep", {"pattern": "TODO", "path": "/tmp/x"}),
        ("PreToolUse", "Glob", {"pattern": "**/*_scanner.py"}),
        ("PreToolUse", "Bash", {"command": "ls"}),
        ("PreToolUse", "Bash", {"command": "echo done > /tmp/x"}),
        ("PreToolUse", "Bash", {"command": "t3 --help"}),
        ("PreToolUse", "TaskCreate", {"subject": "check the build", "description": "check the build"}),
        ("PreToolUse", "mcp__claude_ai_Slack__slack_send_message", {"channel": "D1", "text": "hi"}),
        ("PostToolUse", "Read", {"file_path": "/tmp/x"}),
        ("PostToolUse", "Bash", {"command": "ls"}),
    ],
)
def test_a_tool_call_never_boots_django_the_overlay_or_a_process(
    transcript: Path, attended: dict[str, str], event: str, tool_name: str, tool_input: dict
) -> None:
    payload = {
        "session_id": "cold-pre",
        "transcript_path": str(transcript),
        "tool_name": tool_name,
        "tool_input": tool_input,
    }

    result = _drive(event, payload, attended)

    assert _handlers_run(result.stderr), f"the {event} chain never ran"
    assert result.returncode in {0, 2}
    assert FORBIDDEN not in result.stderr


_TRANSCRIPT_READERS = (
    COLD_GUARD
    + r"""
sys.path.insert(0, sys.argv[1])
from hooks.scripts.hook_router import _is_live_user_turn
from hooks.scripts.managed_repo import teatree_src_on_path
from hooks.scripts.owner_prompts import owner_messages

with teatree_src_on_path():
    from teatree.hooks import verbatim_paste

said = owner_messages(sys.argv[2], limit=verbatim_paste.MAX_RECORDED_MESSAGES)
print(_is_live_user_turn({"transcript_path": sys.argv[2]}))
print(verbatim_paste.scan_body("> " + sys.argv[3], operator_messages=said).outcome)
"""
)


def test_the_transcript_readers_that_replaced_prompt_stamps_stay_cold(tmp_path: Path) -> None:
    said = "please never paste my private chat messages into a public issue again, write your own summary"
    path = tmp_path / "transcript.jsonl"
    entry = {"type": "user", "origin": {"kind": "human"}, "timestamp": datetime.now(tz=UTC).isoformat()}
    path.write_text(json.dumps({**entry, "message": {"role": "user", "content": said}}) + "\n", encoding="utf-8")

    result = subprocess.run(
        [sys.executable, "-c", _TRANSCRIPT_READERS, str(_PLUGIN_ROOT), str(path), said],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert result.stdout.split() == ["True", "reproduced"]
    assert FORBIDDEN not in result.stderr
