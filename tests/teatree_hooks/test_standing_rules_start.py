# test-path: cross-cutting
# Runs hooks/scripts/standing_rules_start.py as the SessionStart hook process it is (no src/teatree mirror).
"""Every standing rule reaches an engaged session the moment it starts, resumes, clears or compacts (#4166).

A session start is a turn that is already happening, so delivering here costs no turn and arms
nothing. The script is its own Django-free process; ``tests/conformance/test_session_start_latency.py``
measures it with the rest of the registered SessionStart chain.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from teatree import standing_directives_cache
from teatree.loop.standing_directives import SCOPE_ATTENDED, compiled_directives
from tests._cold_hook_guard import COLD_SCRIPT_DRIVER, FAILED_WRITE_DRIVER, FORBIDDEN

_HOOKS = Path(__file__).resolve().parents[2] / "hooks"
_SCRIPT = _HOOKS / "scripts" / "standing_rules_start.py"


@pytest.fixture
def state(tmp_path: Path) -> Path:
    directory = tmp_path / "state"
    directory.mkdir()
    return directory


@pytest.fixture
def env(state: Path, tmp_path: Path) -> dict[str, str]:
    """An attended session's hook env, with ``sess-1`` the live session that owns the loop slot."""
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


def _start(
    env: dict[str, str], tmp_path: Path, source: str = "startup", *, session_id: str = "sess-1", failed_write: str = ""
) -> str:
    """The model-visible SessionStart context: ONE nested object, or nothing."""
    driver = [FAILED_WRITE_DRIVER, str(_HOOKS.parent), failed_write] if failed_write else [COLD_SCRIPT_DRIVER]
    result = subprocess.run(
        [sys.executable, "-c", *driver, str(_SCRIPT)],
        input=json.dumps({"session_id": session_id, "source": source, "hook_event_name": "SessionStart"}),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env=env,
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert FORBIDDEN not in result.stderr
    if not result.stdout:
        return ""
    document = json.loads(result.stdout)
    context = document["hookSpecificOutput"]["additionalContext"]
    assert document == {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": context}}
    return context


@pytest.mark.parametrize("source", ["startup", "resume", "clear", "compact"])
def test_every_rule_reaches_an_engaged_session_at_every_start(
    env: dict[str, str], tmp_path: Path, engaged: None, source: str
) -> None:
    context = _start(env, tmp_path, source)

    assert all(directive.text in context for directive in compiled_directives())


def test_a_second_start_delivers_them_again(env: dict[str, str], tmp_path: Path, engaged: None) -> None:
    _start(env, tmp_path)

    assert all(directive.text in _start(env, tmp_path, "compact") for directive in compiled_directives())


def test_the_delivery_restarts_every_interval(env: dict[str, str], tmp_path: Path, state: Path, engaged: None) -> None:
    _start(env, tmp_path)

    stamps = json.loads((state / "sess-1.directives-injected").read_text(encoding="utf-8"))
    assert set(stamps) == {directive.slot_id for directive in compiled_directives()}


def test_the_published_rules_are_what_is_delivered(env: dict[str, str], tmp_path: Path, engaged: None) -> None:
    standing_directives_cache.write(
        [{"slot_id": "slot-a", "cadence_seconds": 120, "text": "Alpha rule.", "scope": SCOPE_ATTENDED}]
    )

    assert (
        _start(env, tmp_path)
        == "Standing rules for this session (1) — they apply to your next reply:\n  - [slot-a] Alpha rule."
    )


def test_a_non_owner_session_never_gets_the_host_wide_slot(env: dict[str, str], tmp_path: Path, state: Path) -> None:
    (state / "sess-2.teatree-active").touch()
    board = next(d.text for d in compiled_directives() if d.slot_id == "standing-pr-board")

    context = _start(env, tmp_path, session_id="sess-2")

    assert all(d.text in context for d in compiled_directives() if d.slot_id != "standing-pr-board")
    assert board not in context


@pytest.mark.parametrize("failure", ["broken-pipe", "unread-pipe"])
def test_a_failed_write_records_nothing(
    env: dict[str, str], tmp_path: Path, state: Path, engaged: None, failure: str
) -> None:
    assert _start(env, tmp_path, failed_write=failure) == ""

    assert not (state / "sess-1.directives-injected").exists()


def test_an_unengaged_session_gets_nothing(env: dict[str, str], tmp_path: Path) -> None:
    assert _start(env, tmp_path) == ""


def test_the_sdk_lane_gets_nothing(env: dict[str, str], tmp_path: Path, engaged: None) -> None:
    assert _start({**env, "CLAUDE_AGENT_SDK_VERSION": "0.1.0"}, tmp_path) == ""


def test_it_is_registered_on_session_start_in_its_own_bounded_process() -> None:
    groups = json.loads((_HOOKS / "hooks.json").read_text(encoding="utf-8"))["hooks"]["SessionStart"]
    commands = {hook["command"]: hook["timeout"] for group in groups for hook in group["hooks"]}

    command = (
        "${CLAUDE_PLUGIN_ROOT}/hooks/scripts/run-hook.sh ${CLAUDE_PLUGIN_ROOT}/hooks/scripts/standing_rules_start.py"
    )
    assert commands[command] == 5
    assert all("matcher" not in group for group in groups if any(h["command"] == command for h in group["hooks"]))
