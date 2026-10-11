"""Keep an MCP stdio client connected while the server process behind it dies and comes back.

``deploy/t3 mcp serve`` runs this as ``python3 -I -S mcp_stdio_proxy.py <server argv>`` on the host,
where teatree is not importable, so it is stdlib-only and runs on macOS's Python 3.9: annotations
that 3.9 would evaluate are quoted. Each respawned child re-runs ``deploy/t3`` and so reaches the
new container; frames pass through byte for byte.
"""

import dataclasses
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from contextlib import suppress
from functools import partial
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any, BinaryIO, cast

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

_LISTEN = "subscriptions/listen"
_INITIALIZE = "initialize"
_DISCOVER = "server/discover"
_CANCELLED = "notifications/cancelled"
_PING = "ping"
_NEGOTIATIONS = (_INITIALIZE, _DISCOVER)
_INITIALIZED = b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n'
_SUBSCRIPTION_ID = "io.modelcontextprotocol/subscriptionId"

INTERNAL_ERROR = -32603
PARSE_ERROR = -32700

BACKOFF_SECONDS = (1.0, 2.0, 4.0, 8.0, 16.0, 30.0)
STABLE_SECONDS = 30.0
_POLL_SECONDS = 0.1
_PARENT_POLL_SECONDS = 1.0
_READ_CHUNK = 1 << 16
_SPAWN_FAILED_EXIT = 127
_FATAL_EXIT = 1
_USAGE_EXIT = 2
_SIGNAL_EXIT_BASE = 128
_STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


def _log(text: str) -> None:
    sys.stderr.write(f"mcp-stdio-proxy: {text}\n")
    sys.stderr.flush()


def _frame(payload: "dict[str, Any]") -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode() + b"\n"


def _error(request_id: object, code: int, message: str, reason: str = "") -> bytes:
    error: dict[str, Any] = {"code": code, "message": message}
    if reason:
        error["data"] = {"retryable": True, "reason": reason}
    return _frame({"jsonrpc": "2.0", "id": request_id, "error": error})


def restarting_error(request_id: object, down_seconds: float, exit_code: int) -> bytes:
    message = (
        f"teatree MCP server is restarting (it exited {down_seconds:.0f}s ago with code {exit_code}); "
        "retry in a few seconds, the session stays connected"
    )
    return _error(request_id, INTERNAL_ERROR, message, "server_restarting")


def restarted_error(request_id: object) -> bytes:
    message = (
        "teatree MCP server restarted while this request was running; it may or may not have taken "
        "effect, re-check before retrying a write"
    )
    return _error(request_id, INTERNAL_ERROR, message, "server_restarted")


def listen_complete(request_id: object) -> bytes:
    result = {"resultType": "complete", "_meta": {_SUBSCRIPTION_ID: request_id}}
    return _frame({"jsonrpc": "2.0", "id": request_id, "result": result})


_PARSE_ERROR_FRAME = _error(None, PARSE_ERROR, "Parse error")


def backoff_delay(failures: int, schedule: "Sequence[float]" = BACKOFF_SECONDS) -> float:
    return 0.0 if failures <= 0 else schedule[min(failures, len(schedule)) - 1]


def _key(request_id: object) -> "tuple[str, object]":
    return (type(request_id).__name__, request_id)


def _is_id(value: object) -> bool:
    return value is None or isinstance(value, (str, int, float))


REQUEST = "request"
NOTIFICATION = "notification"
RESPONSE = "response"
BATCH = "batch"
MALFORMED = "malformed"


@dataclasses.dataclass(frozen=True)
class Message:
    kind: str
    key: "tuple[str, object]" = ("", None)
    request_id: object = None
    method: "str | None" = None
    body: "dict[str, Any] | None" = None


def classify(line: bytes) -> Message:
    try:
        body = json.loads(line)
    except (ValueError, RecursionError):
        return Message(MALFORMED)
    if isinstance(body, list):
        return Message(BATCH)
    method = body.get("method") if isinstance(body, dict) else None
    if not isinstance(body, dict) or not _is_id(body.get("id")) or not isinstance(method, (str, type(None))):
        return Message(MALFORMED)
    if "id" in body:
        kind = RESPONSE if method is None else REQUEST
        return Message(kind, _key(body["id"]), body["id"], method, body)
    if method is None:
        return Message(MALFORMED)
    params = body.get("params")
    target = params.get("requestId") if method == _CANCELLED and isinstance(params, dict) else None
    return Message(NOTIFICATION, _key(target) if _is_id(target) else ("", None), method=method)


def iter_lines(fd: int) -> "Iterator[bytes]":
    pending = bytearray()
    while chunk := os.read(fd, _READ_CHUNK):
        start = len(pending)
        pending += chunk
        end = pending.find(b"\n", start)
        while end != -1:
            yield bytes(pending[: end + 1])
            del pending[: end + 1]
            end = pending.find(b"\n")
    if pending:
        yield bytes(pending)


@dataclasses.dataclass(frozen=True)
class Timing:
    backoff: "tuple[float, ...]" = BACKOFF_SECONDS
    stable_seconds: float = STABLE_SECONDS
    eof_grace: float = 3.0
    kill_grace: float = 2.0
    signal_grace: float = 0.3


@dataclasses.dataclass(frozen=True)
class _Pending:
    request_id: object
    method: "str | None"
    line: bytes = b""


@dataclasses.dataclass(frozen=True)
class _Negotiation:
    line: bytes
    key: "tuple[str, object]"
    method: "str | None"
    version: object


class _Child:
    def __init__(self, proc: "subprocess.Popen[bytes]", generation: int) -> None:
        self.proc = proc
        self.generation = generation
        self.stdin = cast("IO[bytes]", proc.stdin)
        self.stdout = cast("IO[bytes]", proc.stdout)
        self.replayed: _Negotiation | None = None
        self.ready_at: float | None = None

    def write(self, line: bytes) -> None:
        # A child that stopped reading is dying; its exit answers every request it was sent.
        with suppress(OSError, ValueError):
            self.stdin.write(line)
            self.stdin.flush()

    def signal_group(self, signum: int) -> None:
        with suppress(ProcessLookupError, PermissionError):
            os.killpg(self.proc.pid, signum)

    def reap(self) -> int:
        status = self.proc.wait()
        for stream in (self.stdin, self.stdout):
            with suppress(OSError):
                stream.close()
        return status if status >= 0 else _SIGNAL_EXIT_BASE - status


def _launch_dir() -> Path:
    try:
        return Path.cwd()
    except FileNotFoundError:
        return Path("/")


class StdioProxy:
    def __init__(
        self, argv: "Sequence[str]", *, client_in: int, client_out: "BinaryIO", timing: "Timing | None" = None
    ) -> None:
        self._argv = list(argv)
        self._client_in = client_in
        self._client_out = client_out
        self._timing = timing or Timing()
        self._events: queue.SimpleQueue[Callable[[], None]] = queue.SimpleQueue()
        self._launch_dir = _launch_dir()
        self._child: _Child | None = None
        self._generations = 0
        self._inflight: dict[tuple[str, object], _Pending] = {}
        self._held: list[tuple[Message, bytes]] = []
        self._unended: dict[tuple[str, object], object] = {}
        self._negotiation: _Negotiation | None = None
        self._served = False
        self._failures = 0
        self._exited_at = 0.0
        self._exit_status = 0
        self._client_open = True
        self._stop_code: int | None = None
        self._timer: tuple[float, Callable[[], None]] | None = None
        self._exit_code: int | None = None

    def request_stop(self, signum: int, _frame: object = None) -> None:
        self._events.put(partial(self._on_signal, signum))

    def run(self) -> int:
        self._pump(self._client_in, self._on_client_line, self._on_client_eof)
        self._spawn()
        while self._exit_code is None:
            try:
                event = self._events.get(timeout=self._wait_seconds())
            except queue.Empty:
                pass
            else:
                event()
            self._fire_due_timer()
        return self._exit_code

    def _wait_seconds(self) -> float:
        if self._timer is None:
            return _POLL_SECONDS
        return max(0.0, min(_POLL_SECONDS, self._timer[0] - time.monotonic()))

    def _fire_due_timer(self) -> None:
        if self._exit_code is None and self._timer is not None and time.monotonic() >= self._timer[0]:
            action = self._timer[1]
            self._timer = None
            action()

    def _after(self, seconds: float, action: "Callable[[], None]") -> None:
        self._timer = (time.monotonic() + seconds, action)

    def _pump(self, fd: int, on_line: "Callable[[bytes], None]", on_eof: "Callable[[], None]") -> None:
        threading.Thread(target=self._read_into_events, args=(fd, on_line, on_eof), daemon=True).start()

    def _read_into_events(self, fd: int, on_line: "Callable[[bytes], None]", on_eof: "Callable[[], None]") -> None:
        with suppress(OSError):
            for line in iter_lines(fd):
                self._events.put(partial(on_line, line))
        self._events.put(on_eof)

    def _on_signal(self, signum: int) -> None:
        _log(f"received signal {signum}; stopping the server")
        self._stop(signum, code=0)

    def _spawn(self) -> None:
        cwd = self._launch_dir if self._launch_dir.is_dir() else Path("/")
        try:
            proc = subprocess.Popen(
                self._argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, cwd=cwd, start_new_session=True
            )
        except OSError as error:
            _log(f"could not start the server: {error}")
            if self._generations == 0:
                self._exit_code = _SPAWN_FAILED_EXIT
            else:
                self._retry(stable=False)
            return
        self._generations += 1
        child = _Child(proc, self._generations)
        self._child = child
        _log(f"started server pid {proc.pid} (generation {child.generation})")
        self._pump(child.stdout.fileno(), partial(self._on_child_line, child), partial(self._on_child_eof, child))
        negotiation = self._negotiation
        if child.generation == 1 or negotiation is None:
            self._become_ready(child)
            return
        # Only the client's own frame is a safe probe: an unenveloped one locks the handshake era.
        child.replayed = negotiation
        child.write(negotiation.line)

    def _on_replay_reply(self, child: _Child, negotiation: _Negotiation, reply: Message) -> None:
        child.replayed = None
        if negotiation.method == _INITIALIZE:
            result = (reply.body or {}).get("result")
            if not isinstance(result, dict) or result.get("protocolVersion") != negotiation.version:
                _log("the new server refused the session's handshake; exiting so the client reconnects")
                self._stop(signal.SIGTERM, code=_FATAL_EXIT)
                return
            child.write(_INITIALIZED)
        self._become_ready(child)

    def _become_ready(self, child: _Child) -> None:
        child.ready_at = time.monotonic()
        _log(f"server pid {child.proc.pid} is ready")
        for request_id in self._unended.values():
            self._send(listen_complete(request_id))
        self._unended.clear()
        held, self._held = self._held, []
        for message, line in held:
            self._forward(child, message, line)

    def _on_client_line(self, line: bytes) -> None:
        message = classify(line)
        child = self._child
        if child is not None and child.ready_at is not None:
            self._forward(child, message, line)
        else:
            self._answer_while_down(message, line)

    def _forward(self, child: _Child, message: Message, line: bytes) -> None:
        if message.kind == REQUEST:
            remembered = line if message.method in _NEGOTIATIONS else b""
            self._inflight[message.key] = _Pending(message.request_id, message.method, remembered)
        elif message.method == _CANCELLED:
            self._inflight.pop(message.key, None)
        child.write(line)

    def _answer_while_down(self, message: Message, line: bytes) -> None:
        if message.kind == MALFORMED:
            self._send(_PARSE_ERROR_FRAME)
        elif message.method == _CANCELLED:
            self._unended.pop(message.key, None)
            self._held = [(held, raw) for held, raw in self._held if held.key != message.key]
        elif message.kind != REQUEST:
            return
        elif message.method == _LISTEN:
            self._held.append((message, line))
        elif message.method == _PING:
            self._send(_frame({"jsonrpc": "2.0", "id": message.request_id, "result": {}}))
        else:
            down_for = time.monotonic() - self._exited_at
            self._send(restarting_error(message.request_id, down_for, self._exit_status))

    def _on_child_line(self, child: _Child, line: bytes) -> None:
        self._served = True
        message = classify(line)
        replayed = child.replayed
        if message.kind == RESPONSE and replayed is not None and message.key == replayed.key:
            self._on_replay_reply(child, replayed, message)
            return
        if message.kind == RESPONSE:
            self._settle(message)
        self._send(line)

    def _settle(self, reply: Message) -> None:
        pending = self._inflight.pop(reply.key, None)
        result = (reply.body or {}).get("result")
        if pending is not None and result is not None and pending.method in _NEGOTIATIONS:
            version = result.get("protocolVersion") if isinstance(result, dict) else None
            self._negotiation = _Negotiation(pending.line, reply.key, pending.method, version)

    def _on_child_eof(self, child: _Child) -> None:
        child.signal_group(signal.SIGKILL)
        status = child.reap()
        self._child = None
        self._exited_at, self._exit_status = time.monotonic(), status
        _log(f"server pid {child.proc.pid} exited with code {status}")
        if self._stop_code is not None or not self._client_open or not self._served:
            self._exit_code = status if self._stop_code is None else self._stop_code
            return
        for key, pending in self._inflight.items():
            if pending.method == _LISTEN:
                self._unended[key] = pending.request_id
            else:
                self._send(restarted_error(pending.request_id))
        self._inflight.clear()
        stable = child.ready_at is not None and time.monotonic() - child.ready_at >= self._timing.stable_seconds
        self._retry(stable=stable)

    def _retry(self, *, stable: bool) -> None:
        self._failures = 0 if stable else self._failures + 1
        delay = backoff_delay(self._failures, self._timing.backoff)
        _log(f"restarting the server in {delay:g}s")
        self._after(delay, self._spawn)

    def _on_client_eof(self) -> None:
        self._client_open = False
        self._held.clear()
        child = self._child
        if self._stop_code is not None:
            return
        if child is None:
            self._exit_code = 0
            return
        _log("the client closed its input; letting the server finish")
        with suppress(OSError):
            child.stdin.close()
        self._after(self._timing.eof_grace, partial(self._terminate, child))

    def _terminate(self, child: _Child) -> None:
        child.signal_group(signal.SIGTERM)
        self._after(self._timing.kill_grace, partial(self._kill, child))

    def _kill(self, child: _Child) -> None:
        child.signal_group(signal.SIGKILL)
        self._after(self._timing.kill_grace, partial(self._abandon, child))

    def _abandon(self, child: _Child) -> None:
        _log("the server's output never closed; exiting without waiting for it")
        status = child.reap()
        self._exit_code = status if self._stop_code is None else self._stop_code

    def _stop(self, signum: int, *, code: int) -> None:
        child = self._child
        if child is None:
            self._exit_code = code
        elif self._stop_code is not None:
            child.signal_group(signal.SIGKILL)
        else:
            self._stop_code = code
            child.signal_group(signum)
            self._after(self._timing.signal_grace, partial(self._kill, child))

    def _send(self, frame: bytes) -> None:
        try:
            self._client_out.write(frame)
            self._client_out.flush()
        except (OSError, ValueError):
            _log("the client stopped reading; stopping the server")
            self._stop(signal.SIGTERM, code=0)


def watch_parent(
    stop: "Callable[[int], None]",
    getppid: "Callable[[], int]" = os.getppid,
    interval: float = _PARENT_POLL_SECONDS,
) -> threading.Thread:
    launched_by = getppid()

    def watch() -> None:
        while getppid() == launched_by:
            time.sleep(interval)
        _log("the client process is gone; stopping the server")
        stop(signal.SIGTERM)

    thread = threading.Thread(target=watch, name="mcp-stdio-proxy-parent", daemon=True)
    thread.start()
    return thread


def main(
    argv: "Sequence[str]",
    *,
    client_in: int = 0,
    client_out: "BinaryIO | None" = None,
    install: "Callable[[int, Callable[[int, object], None]], object]" = signal.signal,
    getppid: "Callable[[], int]" = os.getppid,
) -> int:
    if not argv:
        _log("usage: python3 mcp_stdio_proxy.py <server command> [arguments...]")
        return _USAGE_EXIT
    proxy = StdioProxy(argv, client_in=client_in, client_out=client_out or sys.stdout.buffer)
    for signum in _STOP_SIGNALS:
        install(signum, proxy.request_stop)
    watch_parent(proxy.request_stop, getppid)
    return proxy.run()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
