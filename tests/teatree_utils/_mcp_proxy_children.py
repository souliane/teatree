"""Child servers and an in-process harness for the MCP stdio proxy tests."""

import json
import os
import queue
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from mcp import types as mcp_types
from mcp.types.version import LATEST_HANDSHAKE_VERSION, LATEST_MODERN_VERSION
from mcp_types._types import CLIENT_CAPABILITIES_META_KEY, CLIENT_INFO_META_KEY, PROTOCOL_VERSION_META_KEY
from pydantic import BaseModel

from teatree.utils.mcp_stdio_proxy import StdioProxy, Timing

MODERN_META = {
    PROTOCOL_VERSION_META_KEY: LATEST_MODERN_VERSION,
    CLIENT_INFO_META_KEY: {"name": "proxy-test", "version": "1"},
    CLIENT_CAPABILITIES_META_KEY: {},
}


def sdk_frame(request_id: object, model: BaseModel) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, **model.model_dump(by_alias=True, exclude_none=True, mode="json")}


def sdk_discover(request_id: object) -> dict[str, Any]:
    return sdk_frame(request_id, mcp_types.DiscoverRequest(params={"_meta": MODERN_META}))


def sdk_call(request_id: object, tool: str, *, modern: bool = True) -> dict[str, Any]:
    meta = MODERN_META if modern else None
    params = mcp_types.CallToolRequestParams(name=tool, arguments={}, _meta=meta)
    return sdk_frame(request_id, mcp_types.CallToolRequest(params=params))


def sdk_listen(request_id: object) -> dict[str, Any]:
    params = mcp_types.SubscriptionsListenRequestParams(
        _meta=MODERN_META, notifications=mcp_types.SubscriptionFilter(tools_list_changed=True)
    )
    return sdk_frame(request_id, mcp_types.SubscriptionsListenRequest(params=params))


def sdk_initialize(request_id: object) -> dict[str, Any]:
    params = mcp_types.InitializeRequestParams(
        protocol_version=LATEST_HANDSHAKE_VERSION,
        capabilities=mcp_types.ClientCapabilities(),
        client_info=mcp_types.Implementation(name="proxy-test", version="1"),
    )
    return sdk_frame(request_id, mcp_types.InitializeRequest(params=params))


def sdk_initialized() -> dict[str, Any]:
    return {"jsonrpc": "2.0", **mcp_types.InitializedNotification().model_dump(by_alias=True, exclude_none=True)}


def tool_pid(frame: dict[str, Any]) -> int:
    return frame["result"]["structuredContent"]["result"]


RETRYABLE = frozenset({"server_restarting", "server_restarted"})


def retry_reason(frame: dict[str, Any]) -> str | None:
    data = frame.get("error", {}).get("data")
    reason = data.get("reason") if isinstance(data, dict) else None
    return reason if reason in RETRYABLE else None


FAST = Timing(backoff=(0.05,), stable_seconds=0.0, eof_grace=3.0, kill_grace=2.0, signal_grace=0.3)

SDK_SERVER = """
import os, time
from mcp.server.mcpserver import MCPServer

server = MCPServer("proxy-probe")

@server.tool()
def echo() -> int:
    return os.getpid()

@server.tool()
def slow() -> int:
    time.sleep(30)
    return os.getpid()

server.run("stdio")
"""

FAKE_SERVER = r"""
import hashlib, json, os, signal, sys, time

log_path = os.environ["FAKE_MCP_LOG"]
plan = json.loads(os.environ["FAKE_MCP_PLAN"])
with open(log_path, encoding="utf-8") as existing:
    spawn = sum(1 for line in existing if '"spawn"' in line)
step = plan[min(spawn, len(plan) - 1)]

def record(**event):
    with open(log_path, "a", encoding="utf-8") as log:
        log.write(json.dumps(event) + "\n")

if step.get("ignore_term"):
    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, signal.SIG_IGN)
grandchild = 0
if step.get("grandchild") or step.get("escape"):
    grandchild = os.fork()
    if grandchild == 0:
        if step.get("escape"):
            os.setsid()
        if step.get("grandchild_closes_stdout"):
            os.close(1)
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            signal.signal(signum, signal.SIG_IGN)
        time.sleep(60)
        os._exit(0)
record(spawn=spawn, pid=os.getpid(), cwd=os.getcwd(), grandchild=grandchild, at=time.time())
if "exit" in step:
    sys.exit(step["exit"])
sys.stderr.write("x" * step.get("stderr", 0))
sys.stderr.flush()
if step.get("close_stdin"):
    os.close(0)
    time.sleep(step.get("linger", 1.0))
    sys.exit(0)
time.sleep(step.get("delay", 0))

def reply(message, result):
    out = {"jsonrpc": "2.0", "id": message["id"], "result": result}
    sys.stdout.buffer.write(json.dumps(out).encode() + b"\n")
    sys.stdout.buffer.flush()

for raw in sys.stdin.buffer:
    record(recv=spawn, line=raw.decode() if len(raw) < 4096 else "<large>")
    try:
        message = json.loads(raw)
    except ValueError:
        sys.stdout.buffer.write(raw)
        sys.stdout.buffer.flush()
        continue
    if "id" not in message or "method" not in message:
        continue
    method = message["method"]
    if method == "subscriptions/listen":
        meta = {"io.modelcontextprotocol/subscriptionId": message["id"]}
        ack = {"jsonrpc": "2.0", "method": "notifications/subscriptions/acknowledged", "params": {"_meta": meta}}
        sys.stdout.buffer.write(json.dumps(ack).encode() + b"\n")
        sys.stdout.buffer.flush()
    elif method == "initialize" and step.get("init_error"):
        refusal = {"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32602, "message": "unsupported"}}
        sys.stdout.buffer.write(json.dumps(refusal).encode() + b"\n")
        sys.stdout.buffer.flush()
    elif method == "initialize":
        version = step.get("version", message["params"]["protocolVersion"])
        reply(message, {"protocolVersion": version, "capabilities": {}, "serverInfo": {"name": "fake", "version": "1"}})
    elif method == "digest":
        reply(message, {"sha256": hashlib.sha256(raw).hexdigest(), "pid": os.getpid()})
    elif method == "big":
        reply(message, {"blob": "y" * message["params"]["size"]})
    elif method == "slow":
        time.sleep(30)
    else:
        reply(message, {"pid": os.getpid(), "spawn": spawn})
    if step.get("close_after_reply"):
        os.close(0)
        time.sleep(step.get("linger", 1.0))
        os._exit(0)
time.sleep(step.get("linger_after_eof", 0))
"""


def write_child(tmp_path: Path, name: str, source: str) -> Path:
    path = tmp_path / name
    path.write_text(source, encoding="utf-8")
    return path


class FakeChild:
    """A stdlib fake MCP server whose per-spawn behaviour comes from a JSON plan."""

    def __init__(self, tmp_path: Path, plan: list[dict[str, Any]]) -> None:
        self.log = tmp_path / "fake-mcp.log"
        self.log.write_text("", encoding="utf-8")
        self.script = write_child(tmp_path, "fake_mcp_server.py", FAKE_SERVER)
        self.env = {"FAKE_MCP_LOG": str(self.log), "FAKE_MCP_PLAN": json.dumps(plan)}

    @property
    def argv(self) -> list[str]:
        assignments = [f"{key}={value}" for key, value in self.env.items()]
        return ["env", *assignments, sys.executable, str(self.script)]

    def events(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]

    def spawns(self) -> list[dict[str, Any]]:
        return [event for event in self.events() if "spawn" in event]

    def received(self, spawn: int) -> list[str]:
        return [event["line"] for event in self.events() if event.get("recv") == spawn]


def request(request_id: object, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    frame: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        frame["params"] = params
    return frame


class Harness:
    """Runs a StdioProxy in a thread with the client's stdin and stdout on pipes."""

    def __init__(self, argv: list[str], timing: Timing = FAST) -> None:
        proxy_in, self._client_write_fd = os.pipe()
        self._client_read_fd, proxy_out = os.pipe()
        self._proxy_in = proxy_in
        self._to_proxy = os.fdopen(self._client_write_fd, "wb")
        self._proxy_out = os.fdopen(proxy_out, "wb")
        self.proxy = StdioProxy(argv, client_in=proxy_in, client_out=self._proxy_out, timing=timing)
        self.raw: queue.Queue[bytes] = queue.Queue()
        self.seen: list[dict[str, Any]] = []
        self.exit_codes: list[int] = []
        self._runner = threading.Thread(target=self._run, daemon=True)
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._runner.start()
        self._reader.start()

    def _run(self) -> None:
        self.exit_codes.append(self.proxy.run())

    def _read(self) -> None:
        with os.fdopen(self._client_read_fd, "rb") as stream:
            for line in stream:
                self.raw.put(line)

    def send(self, frame: dict[str, Any] | bytes) -> None:
        data = frame if isinstance(frame, bytes) else json.dumps(frame).encode() + b"\n"
        self._to_proxy.write(data)
        self._to_proxy.flush()

    def recv_raw(self, timeout: float = 15.0) -> bytes:
        return self.raw.get(timeout=timeout)

    def recv(self, timeout: float = 15.0) -> dict[str, Any]:
        frame = json.loads(self.recv_raw(timeout))
        self.seen.append(frame)
        return frame

    def recv_matching(self, wanted: Callable[[dict[str, Any]], bool], timeout: float = 15.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            frame = self.recv(max(0.01, deadline - time.monotonic()))
            if wanted(frame):
                return frame

    def call_until_served(
        self, build: Callable[[str], dict[str, Any]], timeout: float = 20.0
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Retry a call through a restart; returns the served answer and the errors before it."""
        deadline = time.monotonic() + timeout
        refused: list[dict[str, Any]] = []
        while True:
            request_id = f"retry-{len(refused)}"
            self.send(build(request_id))
            frame = self.recv_matching(lambda f, rid=request_id: f.get("id") == rid, timeout)
            if retry_reason(frame) is None or time.monotonic() > deadline:
                return frame, refused
            refused.append(frame)
            time.sleep(0.1)

    def close_input(self) -> None:
        self._to_proxy.close()

    def wait(self, timeout: float = 15.0) -> int | None:
        self._runner.join(timeout)
        return self.exit_codes[0] if self.exit_codes else None

    def close(self) -> None:
        if self._runner.is_alive():
            self.proxy.request_stop(15)
            self._runner.join(10)
        if not self._to_proxy.closed:
            self._to_proxy.close()
        os.close(self._proxy_in)
        self._proxy_out.close()
        self._reader.join(5)
