"""Worker Codex approval requests cross the real PreToolUse subprocess boundary."""

import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

import pytest
from claude_agent_sdk import ClaudeAgentOptions
from django.test import TestCase

from teatree.agents import codex_app_server_options, harness_registry
from teatree.agents.codex_app_server import CodexAppServerSession, codex_app_server_spec
from teatree.agents.codex_app_server_options import CodexAppServerError, CodexAppServerOptions
from teatree.agents.codex_approval_gate import approval_decision
from teatree.agents.codex_post_tool import report_completed_item
from teatree.agents.codex_shared_app_server import SharedCodexAppServer, SharedCodexSession
from teatree.agents.harness import AgentHarness
from teatree.agents.harness_registry import HarnessBuildContext, HarnessSpec, register_harness, select_harness
from teatree.core.models import Session, Task
from tests.factories import planned_ticket
from tests.teatree_agents._codex_command_shape import codex_wrapped
from tests.teatree_agents._codex_plugin import CODEX_PLUGIN_ID

_SERVER = """
import json
import sys

log = sys.argv[1]
scenario = sys.argv[2]
given = json.loads(sys.argv[3])
threads = 0

def send(message):
    sys.stdout.write(json.dumps(message) + "\\n")
    sys.stdout.flush()

def read_decision():
    while True:
        answer = json.loads(sys.stdin.readline())
        with open(log, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(answer) + "\\n")
        if answer.get("method") == "turn/steer":
            send({"id": answer["id"], "result": {"turnId": "turn-1"}})
            continue
        return answer

for line in sys.stdin:
    request = json.loads(line)
    with open(log, "a", encoding="utf-8") as stream:
        stream.write(json.dumps(request) + "\\n")
    method = request.get("method")
    request_id = request.get("id")
    if method in (None, "initialized", "hang/forever") or request_id is None:
        continue
    if method == "initialize":
        result = {}
    elif method == "account/read":
        result = {"account": {"type": "chatgpt"}}
    elif method in ("thread/start", "thread/resume"):
        threads += 1
        suffix = f"-{scenario}" if scenario in {"read", "blind"} else ""
        result = {"thread": {"id": f"thread-{threads}{suffix}"}}
    elif method == "mcpServerStatus/list":
        result = {"data": [], "nextCursor": None}
    elif method == "turn/start":
        result = {"turn": {"id": "turn-1"}}
    else:
        result = {}
    send({"id": request_id, "result": result})
    if method != "turn/start":
        continue
    if scenario == "inflight":
        send({"id": 500, "method": "item/commandExecution/requestApproval", "params": {
            "threadId": request["params"]["threadId"], "turnId": "turn-1", "itemId": "cmd-500",
            "command": given["pwd"], "cwd": request["params"]["cwd"],
        }})
        continue
    if scenario == "unsupported":
        for offset, name in enumerate(given["unsupported"]):
            send({"id": 130 + offset, "method": name, "params": {
                "threadId": request["params"]["threadId"], "turnId": "turn-1"}})
            read_decision()
        send({"method": "turn/completed", "params": {"threadId": request["params"]["threadId"], "turn": {
            "id": "turn-1", "status": "completed", "items": []}}})
        continue
    if scenario in {"read", "blind"}:
        thread_id = request["params"]["threadId"]
        if scenario == "read":
            send({"id": 110, "method": "item/commandExecution/requestApproval", "params": {
                "threadId": thread_id, "turnId": "turn-1", "itemId": "read-1",
                "command": "cat config.toml", "cwd": request["params"]["cwd"],
            }})
            read_decision()
            send({"method": "item/completed", "params": {"threadId": thread_id, "turnId": "turn-1",
                "item": {"id": "read-1", "type": "commandExecution",
                    "command": "/bin/bash -lc 'cat config.toml'", "commandActions": [{"type": "read",
                        "command": "cat config.toml", "name": "config.toml", "path": "config.toml"}],
                    "cwd": request["params"]["cwd"], "status": "completed", "exitCode": 0,
                    "aggregatedOutput": "existing = true"}}})
        send({"method": "item/fileChange/patchUpdated", "params": {"threadId": thread_id,
            "turnId": "turn-1", "itemId": "config-patch", "changes": [
                {"path": "config.toml", "kind": {"type": "update"}, "diff": "+new = true"}]}})
        send({"id": 111, "method": "item/fileChange/requestApproval", "params": {
            "threadId": thread_id, "turnId": "turn-1", "itemId": "config-patch"}})
        answer = read_decision()
        if answer["result"]["decision"] == "accept":
            send({"method": "item/completed", "params": {"threadId": thread_id, "turnId": "turn-1",
                "item": {"id": "config-patch", "type": "fileChange", "status": "completed", "changes": [
                    {"path": "config.toml", "kind": {"type": "update"}, "diff": "+new = true"}]}}})
        send({"method": "turn/completed", "params": {"threadId": thread_id, "turn": {
            "id": "turn-1", "status": "completed", "items": []}}})
        continue
    if scenario == "delegation":
        if request["params"]["threadId"] == "thread-1":
            send({"method": "item/started", "params": {"threadId": "thread-1", "turnId": "turn-1",
                "item": {"id": "collab-1", "type": "collabAgentToolCall", "tool": "spawnAgent",
                    "senderThreadId": "thread-1", "receiverThreadIds": ["child-1"],
                    "agentsStates": {"child-1": {"status": "running"}}, "status": "inProgress"}}})
            for approval_id, thread_id, command in ((120, "child-1", given["pwd"]),
                                                    (121, "unknown", given["pwd"])):
                send({"id": approval_id, "method": "item/commandExecution/requestApproval", "params": {
                    "threadId": thread_id, "turnId": "turn-1", "itemId": f"cmd-{approval_id}",
                    "command": command, "cwd": request["params"]["cwd"],
                }})
                read_decision()
        else:
            send({"id": 122, "method": "item/commandExecution/requestApproval", "params": {
                "threadId": "thread-2", "turnId": "turn-1", "itemId": "cmd-122",
                "command": given["pwd"], "cwd": request["params"]["cwd"],
            }})
            read_decision()
        send({"method": "turn/completed", "params": {"threadId": request["params"]["threadId"],
            "turn": {"id": "turn-1", "status": "completed", "items": []}}})
        continue
    for approval_id, command in ((100, given["add_all"]), (101, given["pwd"])):
        send({"id": approval_id, "method": "item/commandExecution/requestApproval", "params": {
            "threadId": "thread-1", "turnId": "turn-1", "itemId": f"cmd-{approval_id}",
            "command": command, "cwd": request["params"]["cwd"],
        }})
        read_decision()
    send({"method": "item/started", "params": {"threadId": "thread-1", "turnId": "turn-1",
        "item": {"id": "patch-1", "type": "fileChange", "changes": []}}})
    send({"method": "item/fileChange/patchUpdated", "params": {"threadId": "thread-1",
        "turnId": "turn-1", "itemId": "patch-1", "changes": [
            {"path": "safe.txt", "kind": {"type": "add"}, "diff": "+safe"}]}})
    send({"id": 102, "method": "item/fileChange/requestApproval", "params": {
        "threadId": "thread-1", "turnId": "turn-1", "itemId": "patch-1"}})
    read_decision()
    send({"method": "turn/completed", "params": {"threadId": "thread-1", "turn": {
        "id": "turn-1", "status": "completed", "items": []}}})
"""


_UNSUPPORTED_REQUESTS = (
    "item/tool/requestUserInput",
    "mcpServer/elicitation/request",
    "item/tool/call",
    "account/chatgptAuthTokens/refresh",
    "item/permissions/requestApproval",
)
_GIVEN = {
    "add_all": codex_wrapped("git add -A"),
    "pwd": codex_wrapped("pwd"),
    "unsupported": list(_UNSUPPORTED_REQUESTS),
}


def _server_command(tmp_path: Path, scenario: str) -> tuple[tuple[str, ...], Path]:
    server = tmp_path / "server.py"
    server.write_text(_SERVER)
    log = tmp_path / "requests.jsonl"
    return (sys.executable, str(server), str(log), scenario, json.dumps(_GIVEN)), log


@pytest.fixture
def worker_options(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> CodexAppServerOptions:
    monkeypatch.setattr(codex_app_server_options, "container_is_the_sandbox", lambda: True)
    return CodexAppServerOptions.from_sdk_options(
        ClaudeAgentOptions(cwd=str(tmp_path), permission_mode="bypassPermissions")
    )


def _run_server(tmp_path: Path, options: CodexAppServerOptions, scenario: str = "default") -> list[dict]:
    command, log = _server_command(tmp_path, scenario)
    manager = SharedCodexAppServer(code_home=tmp_path / "home", cache=_FakeCache(), command=command, process_env={})
    session = SharedCodexSession(options, manager=manager, resume=None)

    async def run() -> None:
        await session.start()
        try:
            await session.query("work")
            _ = [message async for message in session.receive_response()]
        finally:
            await session.close()

    try:
        asyncio.run(run())
    finally:
        manager.close()
    return [json.loads(line) for line in log.read_text().splitlines()]


def test_existing_config_requires_completed_read_evidence(
    tmp_path: Path, worker_options: CodexAppServerOptions
) -> None:
    (tmp_path / "config.toml").write_text("existing = true\n")
    read = _run_server(tmp_path, worker_options, "read")
    blind = _run_server(tmp_path, worker_options, "blind")
    assert {message["id"]: message["result"]["decision"] for message in read if "result" in message}[111] == "accept"
    assert {message["id"]: message["result"]["decision"] for message in blind if "result" in message}[111] == "decline"


@pytest.mark.parametrize(("status", "reported"), [("declined", []), ("completed", ["Edit"])])
def test_only_a_file_change_that_ran_reaches_post_tool_use(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, worker_options: CodexAppServerOptions, status: str, reported: list
) -> None:
    sent: list[str] = []

    async def record(tool: str, *_args: object) -> None:
        await asyncio.sleep(0)
        sent.append(tool)

    monkeypatch.setattr("teatree.agents.codex_post_tool._send_post", record)
    item = {
        "type": "fileChange",
        "status": status,
        "cwd": str(tmp_path),
        "changes": [{"path": "config.toml", "kind": {"type": "update"}, "diff": "+x"}],
    }

    asyncio.run(report_completed_item(item, "thread-1", worker_options))

    assert sent == reported


def test_failed_post_tool_report_warns_and_turn_completes(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
    worker_options: CodexAppServerOptions,
) -> None:
    (tmp_path / "config.toml").write_text("existing = true\n")
    monkeypatch.setattr("teatree.agents.codex_router._RUN_HOOK", tmp_path / "missing-hook")

    messages = _run_server(tmp_path, worker_options, "read")

    decisions = {message["id"]: message["result"]["decision"] for message in messages if "result" in message}
    assert decisions[111] == "decline"
    assert "Codex PostToolUse router failed for Bash" in caplog.text


def test_child_and_unknown_approvals_preserve_other_shared_session(
    tmp_path: Path, worker_options: CodexAppServerOptions
) -> None:
    command, log = _server_command(tmp_path, "delegation")
    manager = SharedCodexAppServer(code_home=tmp_path / "home", cache=_FakeCache(), command=command, process_env={})
    first = SharedCodexSession(worker_options, manager=manager, resume=None)
    second = SharedCodexSession(worker_options, manager=manager, resume=None)

    async def run() -> None:
        await first.start()
        await second.start()
        try:
            await first.query("delegate")
            await _consume(first)
            await second.query("continue")
            await _consume(second)
        finally:
            await first.close()
            await second.close()

    async def _consume(session: SharedCodexSession) -> None:
        _ = [message async for message in session.receive_response()]

    try:
        asyncio.run(run())
    finally:
        manager.close()
    messages = [json.loads(line) for line in log.read_text().splitlines()]
    decisions = {message["id"]: message["result"]["decision"] for message in messages if "result" in message}
    assert decisions == {120: "accept", 121: "decline", 122: "accept"}
    assert any(
        message.get("method") == "turn/start" and message["params"]["threadId"] == "thread-2" for message in messages
    )


class _FakeCache:
    @asynccontextmanager
    async def session(self) -> AsyncIterator[Path]:
        yield Path("/unused/auth.json")

    def persist(self) -> None:
        return


def test_worker_request_runs_real_gate_for_command_and_file_change(
    tmp_path: Path, worker_options: CodexAppServerOptions
) -> None:
    messages = _run_server(tmp_path, worker_options)

    thread = next(message for message in messages if message.get("method") == "thread/start")
    assert thread["params"]["approvalPolicy"] == "untrusted"
    assert thread["params"]["sandbox"] == "danger-full-access"
    assert thread["params"]["config"]["plugins"] == {CODEX_PLUGIN_ID: {"enabled": False}}
    decisions = {message["id"]: message["result"]["decision"] for message in messages if "result" in message}
    assert decisions == {100: "decline", 101: "accept", 102: "accept"}
    steer = next(message for message in messages if message.get("method") == "turn/steer")
    assert "git add -A" in steer["params"]["input"][0]["text"]
    contract = json.loads((Path(__file__).parents[1] / "fixtures/codex_app_server/0.155.1-contract.json").read_text())
    for message in messages:
        if method := message.get("method"):
            assert method in contract["methods"]


@pytest.mark.parametrize("script", ["git add -A", "kill 4242"], ids=["stage-all", "pid-kill"])
def test_the_gate_judges_the_script_inside_codexs_shell_wrapper(
    tmp_path: Path, worker_options: CodexAppServerOptions, script: str
) -> None:
    decision, reason = asyncio.run(
        approval_decision(
            "item/commandExecution/requestApproval",
            {"threadId": "thread-1", "command": codex_wrapped(script), "cwd": str(tmp_path)},
            worker_options,
        )
    )

    assert decision == "decline"
    assert reason


def test_only_the_two_approval_methods_get_a_decision_and_every_other_server_request_an_error(
    tmp_path: Path, worker_options: CodexAppServerOptions
) -> None:
    messages = _run_server(tmp_path, worker_options, "unsupported")

    replies = [m for m in messages if m.get("method") is None and m["id"] >= 130]
    assert len(replies) == len(_UNSUPPORTED_REQUESTS)
    for reply in replies:
        assert "result" not in reply
        assert reply["error"]["code"] == -32601


@dataclass
class _ClosedMidApproval:
    close_error: BaseException | None
    pending_error: BaseException | None
    router_alive: bool


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _close_mid_approval(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, options: CodexAppServerOptions
) -> _ClosedMidApproval:
    pid_file = tmp_path / "router.pid"
    router = tmp_path / "slow-router.sh"
    router.write_text(f"#!/bin/sh\necho $$ > {pid_file}\nexec sleep 300\n")
    router.chmod(0o755)
    monkeypatch.setattr("teatree.agents.codex_router._RUN_HOOK", router)
    command, _log = _server_command(tmp_path, "inflight")
    session = CodexAppServerSession(options, resume=None, code_home=tmp_path / "home", command=command, process_env={})

    async def run() -> _ClosedMidApproval:
        await session.start()
        pending = asyncio.create_task(session.request_protocol("hang/forever", {}))
        await session.query("work")
        for _ in range(400):
            if pid_file.exists() and pid_file.read_text().strip():
                break
            await asyncio.sleep(0.05)
        close_error: BaseException | None = None
        try:
            await session.close()
        except CodexAppServerError as exc:
            close_error = exc
        pending_error: BaseException | None = None
        try:
            await asyncio.wait_for(pending, timeout=3)
        except (CodexAppServerError, TimeoutError) as exc:
            pending_error = exc
        return _ClosedMidApproval(close_error, pending_error, _alive(int(pid_file.read_text())))

    try:
        return asyncio.run(run())
    finally:
        if pid_file.exists():
            with suppress(ProcessLookupError):
                os.kill(int(pid_file.read_text()), signal.SIGKILL)


def test_closing_a_session_mid_approval_raises_nothing_and_fails_the_pending_requests(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, worker_options: CodexAppServerOptions
) -> None:
    closed = _close_mid_approval(monkeypatch, tmp_path, worker_options)

    assert closed.close_error is None
    assert isinstance(closed.pending_error, CodexAppServerError)


def test_closing_a_session_mid_approval_kills_the_router_it_was_waiting_on(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, worker_options: CodexAppServerOptions
) -> None:
    closed = _close_mid_approval(monkeypatch, tmp_path, worker_options)

    assert closed.router_alive is False


@pytest.mark.parametrize(
    ("hook_body", "expected"),
    [
        ("exit 0", "accept"),
        ("exit 1", "decline"),
        ("echo 'not json'", "decline"),
        ("""echo '{"permissionDecision": "ask"}'""", "decline"),
        ("echo 'Traceback (most recent call last)' >&2", "decline"),
        ("echo 'WARNING: teatree hooks are DISABLED' >&2", "decline"),
    ],
    ids=[
        "silent-success-accepts",
        "crash-declines",
        "unreadable-declines",
        "no-allow-declines",
        "traceback-on-stderr-declines",
        "hooks-disabled-on-stderr-declines",
    ],
)
def test_the_router_declines_only_when_it_fails_or_says_something_other_than_allow(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    worker_options: CodexAppServerOptions,
    hook_body: str,
    expected: str,
) -> None:
    hook = tmp_path / "router.sh"
    hook.write_text(f"#!/bin/sh\n{hook_body}\n")
    hook.chmod(0o755)
    monkeypatch.setattr("teatree.agents.codex_router._RUN_HOOK", hook)

    decision, _reason = asyncio.run(
        approval_decision(
            "item/commandExecution/requestApproval",
            {"threadId": "thread-1", "command": "pwd", "cwd": str(tmp_path)},
            worker_options,
        )
    )

    assert decision == expected


def test_router_start_failure_declines_worker_approval(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, worker_options: CodexAppServerOptions
) -> None:
    monkeypatch.setattr("teatree.agents.codex_router._RUN_HOOK", tmp_path / "missing-hook")

    decision, reason = asyncio.run(
        approval_decision(
            "item/commandExecution/requestApproval",
            {"threadId": "thread-1", "command": "pwd", "cwd": str(tmp_path)},
            worker_options,
        )
    )

    assert decision == "decline"
    assert "router could not evaluate" in reason


class TestMainCloneRoute(TestCase):
    def test_worker_architectural_review_in_managed_main_clone_routes_to_claude(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            clone = Path(directory) / "clone"
            clone.mkdir()
            git = shutil.which("git")
            assert git is not None
            subprocess.run([git, "init", "-q", "-b", "main", str(clone)], check=True)
            subprocess.run(
                [git, "-C", str(clone), "remote", "add", "origin", "https://github.com/souliane/teatree.git"],
                check=True,
            )
            ticket = planned_ticket()
            task = Task.objects.create(
                ticket=ticket, session=Session.objects.create(ticket=ticket), phase="architectural_review"
            )
            codex = codex_app_server_spec()
            register_harness(
                HarnessSpec(
                    name="codex_main_clone_probe", factory=codex.factory, unavailable_reason=codex.unavailable_reason
                )
            )
            try:
                with (
                    patch("teatree.agents.codex_app_server.container_is_the_sandbox", return_value=True),
                    patch("teatree.agents.codex_app_server.shutil.which", return_value="/usr/bin/codex"),
                    patch.dict("os.environ", {"T3_REPO": str(clone)}),
                ):
                    selected = select_harness(
                        ["codex_main_clone_probe", AgentHarness.CLAUDE_SDK.value],
                        HarnessBuildContext(task=task, phase="architectural_review"),
                    )
            finally:
                harness_registry._REGISTRY.pop("codex_main_clone_probe", None)

        assert selected.spec.name == AgentHarness.CLAUDE_SDK.value
        assert "main clone" in selected.rejected[0].reason
