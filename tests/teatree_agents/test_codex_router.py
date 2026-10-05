"""The one place a Codex action reaches the hook router, and the one policy for stopping it."""

import asyncio
import gc
import os
import signal
import sys
import time
from contextlib import suppress
from pathlib import Path

import pytest

from teatree.agents.codex_router import run_router


def _router_script(tmp_path: Path, body: str) -> Path:
    script = tmp_path / "router.sh"
    script.write_text(f"#!/bin/sh\n{body}\n")
    script.chmod(0o755)
    return script


def test_the_router_run_reports_its_exit_and_both_streams(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "teatree.agents.codex_router._RUN_HOOK",
        _router_script(tmp_path, "cat >/dev/null; echo out; echo err >&2; exit 2"),
    )

    run = asyncio.run(run_router("PreToolUse", {"tool_name": "Bash"}, str(tmp_path)))

    assert run is not None
    assert (run.returncode, run.stdout, run.stderr) == (2, b"out\n", b"err\n")


def test_the_router_receives_the_event_and_the_payload_on_stdin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen = tmp_path / "stdin.json"
    monkeypatch.setattr("teatree.agents.codex_router._RUN_HOOK", _router_script(tmp_path, f"cat > {seen}"))

    asyncio.run(run_router("PostToolUse", {"tool_name": "Bash"}, str(tmp_path)))

    assert seen.read_text() == f'{{"tool_name": "Bash", "hook_event_name": "PostToolUse", "cwd": "{tmp_path}"}}'


def test_a_router_that_cannot_start_reports_nothing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("teatree.agents.codex_router._RUN_HOOK", tmp_path / "missing-hook")

    assert asyncio.run(run_router("PreToolUse", {}, str(tmp_path))) is None


@pytest.mark.parametrize("marker", ["Traceback (most recent call last)", "teatree hooks are DISABLED"])
def test_a_router_that_says_it_crashed_or_is_disabled_is_marked_crashed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, marker: str
) -> None:
    monkeypatch.setattr("teatree.agents.codex_router._RUN_HOOK", _router_script(tmp_path, f"echo '{marker}' >&2"))

    run = asyncio.run(run_router("PreToolUse", {}, str(tmp_path)))

    assert run is not None
    assert run.crashed


def test_a_caller_that_stops_waiting_takes_the_router_down_with_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pid_file = tmp_path / "router.pid"
    monkeypatch.setattr(
        "teatree.agents.codex_router._RUN_HOOK", _router_script(tmp_path, f"echo $$ > {pid_file}\nexec sleep 300")
    )

    async def cancel_mid_run() -> None:
        task = asyncio.create_task(run_router("PreToolUse", {}, str(tmp_path)))
        for _ in range(200):
            if pid_file.exists() and pid_file.read_text().strip():
                break
            await asyncio.sleep(0.05)
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    try:
        asyncio.run(cancel_mid_run())
        with pytest.raises(ProcessLookupError):
            os.kill(int(pid_file.read_text()), 0)
    finally:
        if pid_file.exists():
            with suppress(ProcessLookupError):
                os.kill(int(pid_file.read_text()), signal.SIGKILL)


def test_a_caller_that_stops_waiting_takes_the_routers_children_down_too(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    child_pid = tmp_path / "child.pid"
    monkeypatch.setattr(
        "teatree.agents.codex_router._RUN_HOOK", _router_script(tmp_path, f"sleep 300 &\necho $! > {child_pid}\nwait")
    )

    async def cancel_mid_run() -> bool:
        task = asyncio.create_task(run_router("PreToolUse", {}, str(tmp_path)))
        for _ in range(200):
            if child_pid.exists() and child_pid.read_text(encoding="utf-8").strip():
                break
            await asyncio.sleep(0.05)
        task.cancel()
        finished, _pending = await asyncio.wait({task}, timeout=10)
        return bool(finished)

    try:
        assert asyncio.run(cancel_mid_run()) is True
        child = int(child_pid.read_text(encoding="utf-8"))
        for _ in range(40):
            try:
                os.kill(child, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        with pytest.raises(ProcessLookupError):
            os.kill(child, 0)
    finally:
        if child_pid.exists():
            with suppress(ProcessLookupError):
                os.kill(int(child_pid.read_text(encoding="utf-8")), signal.SIGKILL)


def test_a_router_that_exits_leaving_a_child_on_its_pipes_is_declined_and_the_child_killed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    child_pid = tmp_path / "child.pid"
    monkeypatch.setattr("teatree.agents.codex_router._TIMEOUT_SECONDS", 1)
    monkeypatch.setattr(
        "teatree.agents.codex_router._RUN_HOOK", _router_script(tmp_path, f"sleep 300 &\necho $! > {child_pid}\nexit 0")
    )

    try:
        assert asyncio.run(run_router("PreToolUse", {}, str(tmp_path))) is None
        child = int(child_pid.read_text(encoding="utf-8"))
        for _ in range(40):
            try:
                os.kill(child, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        with pytest.raises(ProcessLookupError):
            os.kill(child, 0)
    finally:
        if child_pid.exists():
            with suppress(ProcessLookupError):
                os.kill(int(child_pid.read_text(encoding="utf-8")), signal.SIGKILL)


@pytest.mark.parametrize("leader", ["exit 0", "wait"], ids=["leader-exits", "leader-waits"])
def test_a_router_stopped_by_the_timeout_leaves_no_transport_for_the_garbage_collector(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, leader: str
) -> None:
    monkeypatch.setattr("teatree.agents.codex_router._TIMEOUT_SECONDS", 0.3)
    monkeypatch.setattr(
        "teatree.agents.codex_router._RUN_HOOK",
        _router_script(tmp_path, f"sleep 300 &\necho $! > {tmp_path}/pid\n{leader}"),
    )
    unraisable: list[object] = []
    monkeypatch.setattr(sys, "unraisablehook", unraisable.append)
    try:
        for _ in range(5):
            assert asyncio.run(run_router("PreToolUse", {}, str(tmp_path))) is None
            gc.collect()
    finally:
        pid_file = tmp_path / "pid"
        if pid_file.exists():
            with suppress(ProcessLookupError):
                os.kill(int(pid_file.read_text(encoding="utf-8")), signal.SIGKILL)

    assert unraisable == []
