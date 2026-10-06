"""An unsandboxed Codex thread is only as safe as the requests the approval gate can see."""

import asyncio
import json
import logging
import sys
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from claude_agent_sdk import ClaudeAgentOptions

from teatree.agents import codex_app_server_events, codex_app_server_options
from teatree.agents.codex_app_server import CodexAppServerSession
from teatree.agents.codex_app_server_options import CodexAppServerError, CodexAppServerOptions
from teatree.agents.codex_shared_app_server import SharedCodexAppServer, SharedCodexSession
from teatree.agents.harness_registry import HarnessFallbackError, HarnessFallbackKind

_SERVER = """
import json
import sys

log, scenario, servers = sys.argv[1], sys.argv[2], json.loads(sys.argv[3])
threads = 0


def send(message):
    sys.stdout.write(json.dumps(message) + "\\n")
    sys.stdout.flush()


def read_reply():
    while True:
        answer = json.loads(sys.stdin.readline())
        with open(log, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(answer) + "\\n")
        if answer.get("method") == "turn/steer":
            send({"id": answer["id"], "result": {"turnId": "turn-1"}})
            continue
        return answer


def complete(thread_id):
    send({"method": "turn/completed", "params": {"threadId": thread_id,
        "turn": {"id": "turn-1", "status": "completed", "items": []}}})


for line in sys.stdin:
    request = json.loads(line)
    with open(log, "a", encoding="utf-8") as stream:
        stream.write(json.dumps(request) + "\\n")
    method, request_id = request.get("method"), request.get("id")
    if method in (None, "initialized") or request_id is None:
        continue
    result = {}
    if method == "account/read":
        result = {"account": {"type": "chatgpt"}}
    elif method in ("thread/start", "thread/resume"):
        threads += 1
        result = {"thread": {"id": f"thread-{threads}"}}
    elif method == "mcpServerStatus/list":
        if scenario == "status_hang":
            continue
        if scenario == "error_status":
            send({"id": request_id, "error": {"code": -32601, "message": "no such method"}})
            continue
        if scenario == "auth_status":
            error = {"code": -32000, "message": "x", "data": {"codexErrorInfo": "unauthorized"}}
            send({"id": request_id, "error": error})
            continue
        if scenario == "garbage_status":
            sys.stdout.write("not-json\\n")
            sys.stdout.flush()
            continue
        listed = [] if scenario == "lock" and request["params"]["threadId"] != "thread-1" else servers
        result = {"garbled_status": {}, "scalar_status": {"data": 5}}.get(
            scenario, {"data": [{"name": name} for name in listed], "nextCursor": None}
        )
    elif method == "thread/unsubscribe":
        if scenario in ("lock", "unsubscribe_hang"):
            continue
        if scenario == "unsubscribe_error":
            send({"id": request_id, "error": {"code": -32601, "message": "no such method"}})
            continue
    elif method == "turn/start":
        result = {"turn": {"id": "turn-1"}}
    send({"id": request_id, "result": result})
    if method != "turn/start":
        continue
    thread_id = request["params"]["threadId"]
    if scenario == "odd_method":
        send({"id": 140, "method": ["not", "a", "string"], "params": {}})
        read_reply()
    elif scenario == "approval":
        send({"id": 150, "method": "item/commandExecution/requestApproval", "params": {
            "threadId": thread_id, "turnId": "turn-1", "itemId": "cmd-150", "command": "pwd",
            "cwd": request["params"]["cwd"]}})
        read_reply()
    elif scenario == "slow_reports" and thread_id == "thread-1":
        send({"method": "item/completed", "params": {"threadId": thread_id, "turnId": "turn-1", "item": {
            "id": "cmd-1", "type": "commandExecution", "command": "pwd", "cwd": request["params"]["cwd"],
            "status": "completed", "exitCode": 0, "aggregatedOutput": ""}}})
    complete(thread_id)
"""


class _FakeCache:
    @asynccontextmanager
    async def session(self) -> AsyncIterator[Path]:
        yield Path("/unused/auth.json")

    def persist(self) -> None:
        return


def _command(tmp_path: Path, scenario: str, servers: tuple[str, ...] = ()) -> tuple[tuple[str, ...], Path]:
    script = tmp_path / "server.py"
    script.write_text(_SERVER)
    log = tmp_path / "requests.jsonl"
    return (sys.executable, str(script), str(log), scenario, json.dumps(list(servers))), log


def _logged_methods(log: Path) -> list[object]:
    try:
        return [request.get("method") for request in _requests(log)]
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _requests(log: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def unsandboxed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> CodexAppServerOptions:
    monkeypatch.setattr(codex_app_server_options, "container_is_the_sandbox", lambda: True)
    return CodexAppServerOptions.from_sdk_options(
        ClaudeAgentOptions(cwd=str(tmp_path), permission_mode="bypassPermissions")
    )


@pytest.fixture
def host_sandboxed(tmp_path: Path) -> CodexAppServerOptions:
    return CodexAppServerOptions.from_sdk_options(
        ClaudeAgentOptions(cwd=str(tmp_path), permission_mode="bypassPermissions")
    )


def _direct(tmp_path: Path, options: CodexAppServerOptions, scenario: str, servers: tuple[str, ...] = ()) -> tuple:
    command, log = _command(tmp_path, scenario, servers)
    return CodexAppServerSession(
        options, resume=None, code_home=tmp_path / "home", command=command, process_env={}
    ), log


def _shared(tmp_path: Path, options: CodexAppServerOptions, scenario: str, servers: tuple[str, ...] = ()) -> tuple:
    command, log = _command(tmp_path, scenario, servers)
    manager = SharedCodexAppServer(code_home=tmp_path / "home", cache=_FakeCache(), command=command, process_env={})
    return SharedCodexSession(options, manager=manager, resume=None), log, manager


def _open(kind: str, tmp_path: Path, options: CodexAppServerOptions, scenario: str, servers: tuple[str, ...] = ()):
    if kind == "direct":
        session, log = _direct(tmp_path, options, scenario, servers)
        return session, log, None
    return _shared(tmp_path, options, scenario, servers)


async def _drive(session: CodexAppServerSession | SharedCodexSession) -> None:
    await session.start()
    try:
        await session.query("work")
        _ = [message async for message in session.receive_response()]
    finally:
        await session.close()


def _run(kind: str, tmp_path: Path, options: CodexAppServerOptions, scenario: str, servers: tuple[str, ...] = ()):
    session, log, manager = _open(kind, tmp_path, options, scenario, servers)
    try:
        asyncio.run(_drive(session))
    finally:
        if manager is not None:
            manager.close()
    return _requests(log)


@pytest.mark.parametrize("kind", ["direct", "shared"])
class TestAnUnsandboxedThreadMustNotLoadAnyMcpServer:
    def test_a_thread_that_still_loads_a_server_is_refused_as_a_fallback_before_any_turn(
        self, tmp_path: Path, unsandboxed: CodexAppServerOptions, kind: str
    ) -> None:
        session, log, manager = _open(kind, tmp_path, unsandboxed, "mcp", ("codex_apps", "teatree"))

        async def start() -> None:
            await session.start()

        try:
            with pytest.raises(HarnessFallbackError) as refused:
                asyncio.run(start())
        finally:
            if manager is not None:
                manager.close()

        assert refused.value.kind is HarnessFallbackKind.ACCESS
        assert refused.value.side_effects_started is False
        assert "codex_apps, teatree" in str(refused.value)
        assert not [request for request in _requests(log) if request.get("method") == "turn/start"]

    def test_a_refused_thread_is_unsubscribed_on_the_shared_server_so_codex_unloads_it(
        self, tmp_path: Path, unsandboxed: CodexAppServerOptions, kind: str
    ) -> None:
        session, log, manager = _open(kind, tmp_path, unsandboxed, "mcp", ("codex_apps",))

        async def start() -> None:
            await session.start()

        try:
            with pytest.raises(HarnessFallbackError):
                asyncio.run(start())
        finally:
            if manager is not None:
                manager.close()

        unsubscribes = [request for request in _requests(log) if request.get("method") == "thread/unsubscribe"]
        assert [request["params"] for request in unsubscribes] == (
            [{"threadId": "thread-1"}] if kind == "shared" else []
        )

    @pytest.mark.parametrize("scenario", ["garbled_status", "scalar_status", "error_status"])
    def test_a_status_the_probe_cannot_read_is_refused_as_a_fallback_too(
        self, tmp_path: Path, unsandboxed: CodexAppServerOptions, kind: str, scenario: str
    ) -> None:
        session, _log, manager = _open(kind, tmp_path, unsandboxed, scenario)

        async def start() -> None:
            await session.start()

        try:
            with pytest.raises(HarnessFallbackError, match="could not be read") as refused:
                asyncio.run(start())
        finally:
            if manager is not None:
                manager.close()

        assert refused.value.kind is HarnessFallbackKind.ACCESS

    def test_a_thread_with_no_server_opens_after_being_asked_about_itself(
        self, tmp_path: Path, unsandboxed: CodexAppServerOptions, kind: str
    ) -> None:
        requests = _run(kind, tmp_path, unsandboxed, "mcp")

        probe = next(request for request in requests if request.get("method") == "mcpServerStatus/list")
        assert probe["params"] == {"threadId": "thread-1", "detail": "toolsAndAuthOnly"}
        assert any(request.get("method") == "turn/start" for request in requests)

    def test_a_host_thread_keeps_its_mcp_servers_and_is_not_probed(
        self, tmp_path: Path, host_sandboxed: CodexAppServerOptions, kind: str
    ) -> None:
        requests = _run(kind, tmp_path, host_sandboxed, "mcp", ("teatree",))

        assert not [request for request in requests if request.get("method") == "mcpServerStatus/list"]
        assert any(request.get("method") == "turn/start" for request in requests)


def test_a_server_request_with_a_method_that_is_not_a_string_is_refused_and_the_turn_carries_on(
    tmp_path: Path, unsandboxed: CodexAppServerOptions
) -> None:
    requests = _run("direct", tmp_path, unsandboxed, "odd_method")

    reply = next(request for request in requests if request.get("id") == 140 and "method" not in request)
    assert reply["error"]["code"] == -32601


def test_a_server_request_that_cannot_be_answered_is_logged(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
    unsandboxed: CodexAppServerOptions,
) -> None:
    async def broken(*_args: object) -> tuple[str, str | None]:
        await asyncio.sleep(0)
        msg = "router exploded"
        raise RuntimeError(msg)

    monkeypatch.setattr("teatree.agents.codex_app_server.approval_decision", broken)

    with caplog.at_level(logging.WARNING):
        requests = _run("direct", tmp_path, unsandboxed, "approval")

    assert next(request for request in requests if request.get("id") == 150)["result"] == {"decision": "decline"}
    assert "Codex server request could not be answered: RuntimeError" in caplog.text


def test_one_threads_slow_reports_do_not_hold_up_another_thread(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, unsandboxed: CodexAppServerOptions
) -> None:
    hook = tmp_path / "router.sh"
    hook.write_text("#!/bin/sh\nif grep -q 'thread-1'; then sleep 20; fi\n")
    hook.chmod(0o755)
    monkeypatch.setattr("teatree.agents.codex_router._RUN_HOOK", hook)
    command, _log = _command(tmp_path, "slow_reports")
    manager = SharedCodexAppServer(code_home=tmp_path / "home", cache=_FakeCache(), command=command, process_env={})
    slow = SharedCodexSession(unsandboxed, manager=manager, resume=None)
    quick = SharedCodexSession(unsandboxed, manager=manager, resume=None)

    async def consume(session: SharedCodexSession) -> None:
        _ = [message async for message in session.receive_response()]

    async def run() -> tuple[float, float]:
        await slow.start()
        await quick.start()
        await slow.query("slow")
        await quick.query("quick")
        started = time.monotonic()
        await asyncio.wait_for(consume(quick), timeout=8)
        quick_seconds = time.monotonic() - started
        monkeypatch.setattr(codex_app_server_events, "POST_SETTLE_SECONDS", 1.0)
        started = time.monotonic()
        await asyncio.wait_for(consume(slow), timeout=15)
        await slow.close()
        await quick.close()
        return quick_seconds, time.monotonic() - started

    try:
        quick_seconds, slow_seconds = asyncio.run(run())
    finally:
        started = time.monotonic()
        manager.close()
        closing_seconds = time.monotonic() - started

    assert quick_seconds < 8
    assert slow_seconds < 10
    assert closing_seconds < 5


def test_the_managers_close_outlasts_a_report_that_is_still_draining(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, unsandboxed: CodexAppServerOptions
) -> None:
    hook = tmp_path / "router.sh"
    started = tmp_path / "report.started"
    hook.write_text(f"#!/bin/sh\nif grep -q 'thread-1'; then touch {started}; sleep 60; fi\n")
    hook.chmod(0o755)
    monkeypatch.setattr("teatree.agents.codex_router._RUN_HOOK", hook)
    monkeypatch.setattr(codex_app_server_events, "POST_SETTLE_SECONDS", 12.0)
    command, _log = _command(tmp_path, "slow_reports")
    manager = SharedCodexAppServer(code_home=tmp_path / "home", cache=_FakeCache(), command=command, process_env={})
    session = SharedCodexSession(unsandboxed, manager=manager, resume=None)

    async def start_a_slow_report() -> None:
        await session.start()
        await session.query("slow")
        for _ in range(200):
            if started.exists():
                return
            await asyncio.sleep(0.05)

    try:
        asyncio.run(start_a_slow_report())
        manager.close()
        assert manager.stopped
    finally:
        manager.close()


def test_a_status_request_that_never_answers_refuses_the_thread_within_the_bound(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, unsandboxed: CodexAppServerOptions
) -> None:
    monkeypatch.setattr("teatree.agents.codex_mcp_probe.MCP_PROBE_SECONDS", 0.5)
    session, _log, manager = _open("shared", tmp_path, unsandboxed, "status_hang")

    async def start() -> None:
        await asyncio.wait_for(session.start(), timeout=15)

    started = time.monotonic()
    try:
        with pytest.raises(HarnessFallbackError, match="did not answer in time") as refused:
            asyncio.run(start())
    finally:
        if manager is not None:
            manager.close()

    assert refused.value.kind is HarnessFallbackKind.ACCESS
    assert time.monotonic() - started < 8


@pytest.mark.parametrize("kind", ["direct", "shared"])
def test_a_protocol_failure_while_reading_the_status_is_not_recorded_as_an_unreadable_status(
    tmp_path: Path, unsandboxed: CodexAppServerOptions, kind: str
) -> None:
    session, _log, manager = _open(kind, tmp_path, unsandboxed, "garbage_status")
    command, _log = _command(tmp_path, "garbage_status")
    managers = [] if manager is None else [manager]

    def fresh_manager(code_home: Path) -> SharedCodexAppServer:
        managers.append(SharedCodexAppServer(code_home=code_home, cache=_FakeCache(), command=command, process_env={}))
        return managers[-1]

    async def start() -> None:
        await session.start()

    try:
        with (
            patch("teatree.agents.codex_shared_app_server.shared_codex_app_server", side_effect=fresh_manager),
            pytest.raises(CodexAppServerError, match="emitted an invalid JSONL protocol message") as failed,
        ):
            asyncio.run(start())
    finally:
        for opened in managers:
            opened.close()

    assert not isinstance(failed.value, HarnessFallbackError)
    assert "could not be read" not in str(failed.value)


def test_a_failing_unsubscribe_neither_hides_the_refusal_nor_escapes(
    tmp_path: Path, unsandboxed: CodexAppServerOptions
) -> None:
    session, log, manager = _shared(tmp_path, unsandboxed, "unsubscribe_error", ("codex_apps",))

    async def start() -> None:
        await session.start()

    try:
        with pytest.raises(HarnessFallbackError, match="codex_apps"):
            asyncio.run(start())
    finally:
        manager.close()

    assert [r["params"] for r in _requests(log) if r.get("method") == "thread/unsubscribe"] == [
        {"threadId": "thread-1"}
    ]


def test_an_unsubscribe_that_never_answers_does_not_hold_up_another_thread_opening(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, unsandboxed: CodexAppServerOptions
) -> None:
    monkeypatch.setattr("teatree.agents.codex_shared_app_server.UNSUBSCRIBE_SECONDS", 4.0)
    refused, log, manager = _shared(tmp_path, unsandboxed, "lock", ("codex_apps",))
    opened = SharedCodexSession(unsandboxed, manager=manager, resume=None)

    async def run() -> float:
        refusal = asyncio.create_task(refused.start())
        for _ in range(200):
            if "thread/unsubscribe" in _logged_methods(log):
                break
            await asyncio.sleep(0.05)
        else:
            pytest.fail("the refused session never sent thread/unsubscribe")
        started = time.monotonic()
        await asyncio.wait_for(opened.start(), timeout=2.5)
        elapsed = time.monotonic() - started
        with pytest.raises(HarnessFallbackError):
            await asyncio.wait_for(refusal, timeout=10)
        await opened.close()
        return elapsed

    try:
        assert asyncio.run(run()) < 2.5
    finally:
        manager.close()


def test_a_typed_provider_fallback_on_the_status_request_still_unsubscribes_the_thread(
    tmp_path: Path, unsandboxed: CodexAppServerOptions
) -> None:
    session, log, manager = _shared(tmp_path, unsandboxed, "auth_status")

    async def start() -> None:
        await session.start()

    try:
        with pytest.raises(HarnessFallbackError) as refused:
            asyncio.run(start())
    finally:
        manager.close()

    assert refused.value.kind is HarnessFallbackKind.AUTH
    assert [r["params"] for r in _requests(log) if r.get("method") == "thread/unsubscribe"] == [
        {"threadId": "thread-1"}
    ]
