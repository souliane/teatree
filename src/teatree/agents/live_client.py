"""Operator-side client for the live sessions every worker on this host publishes.

A worker's socket file is removed only when nothing listens on it (``ECONNREFUSED``); any other
failure is reported against its name and the file is left alone.
"""

import json
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import NotRequired, TypedDict, cast

from teatree.agents import live_mailbox
from teatree.agents.live_control import DEFAULT_STEER_WAIT_SECONDS, ReceiptPayload, SessionFacts

_CONNECT_SECONDS = 5.0
_PASSIVE_SECONDS = 10.0
_STEER_GRACE_SECONDS = 20.0
_MAX_RESPONSE_BYTES = 4_000_000


class WorkerSessionFacts(SessionFacts):
    worker: str


class LiveRequest(TypedDict):
    method: str
    task: NotRequired[int]
    text: NotRequired[str]
    command_id: NotRequired[str]
    wait_seconds: NotRequired[float]


class LiveOfflineError(Exception):
    """No worker on this host answered, or none is running the task."""


class LiveRefusedError(Exception):
    def __init__(self, message: str, code: str | None) -> None:
        super().__init__(message)
        self.code = code


class LiveDeliveryUnknownError(Exception):
    """The steer was sent but its answer never arrived; it may or may not have been accepted."""


class _UnreachableError(Exception):
    pass


class _AnswerLostError(Exception):
    pass


@dataclass
class LiveClient:
    live_dir: Path | None = None
    unreachable: dict[str, str] = field(default_factory=dict)

    def workers(self) -> list[Path]:
        directory = self.live_dir or live_mailbox.live_dir()
        return sorted(directory.glob("*.sock")) if directory.is_dir() else []

    def sessions(self) -> list[WorkerSessionFacts]:
        """Every live session on every answering worker, each tagged with its worker."""
        rows: list[WorkerSessionFacts] = []
        answered = 0
        for worker in self.workers():
            try:
                listed = self._call(worker, {"method": "live.list"}, timeout=_PASSIVE_SECONDS)
            except (_UnreachableError, _AnswerLostError) as exc:
                self.unreachable[worker.stem] = str(exc)
                continue
            answered += 1
            rows.extend(
                cast("WorkerSessionFacts", {**row, "worker": worker.stem}) for row in cast("list[SessionFacts]", listed)
            )
        if not answered:
            reasons = ", ".join(f"{name} ({why})" for name, why in self.unreachable.items()) or "no worker socket"
            msg = f"no live worker answered on this host: {reasons}"
            raise LiveOfflineError(msg)
        return rows

    def inspect(self, task: int) -> WorkerSessionFacts:
        worker = self._owner(task)
        try:
            facts = self._call(worker, {"method": "live.inspect", "task": task}, timeout=_PASSIVE_SECONDS)
        except (_UnreachableError, _AnswerLostError) as exc:
            msg = f"worker {worker.stem} stopped answering: {exc}"
            raise LiveOfflineError(msg) from exc
        return cast("WorkerSessionFacts", {**cast("SessionFacts", facts), "worker": worker.stem})

    def steer(
        self,
        task: int,
        text: str,
        *,
        command_id: str,
        wait: float = DEFAULT_STEER_WAIT_SECONDS,
    ) -> ReceiptPayload:
        worker = self._owner(task)
        request: LiveRequest = {
            "method": "live.steer",
            "task": task,
            "text": text,
            "command_id": command_id,
            "wait_seconds": wait,
        }
        try:
            return cast("ReceiptPayload", self._call(worker, request, timeout=wait + _STEER_GRACE_SECONDS))
        except _UnreachableError as exc:
            msg = f"worker {worker.stem} stopped answering before the steer was sent: {exc}"
            raise LiveOfflineError(msg) from exc
        except _AnswerLostError as exc:
            raise LiveDeliveryUnknownError(str(exc)) from exc

    def _owner(self, task: int) -> Path:
        for row in self.sessions():
            if row["task"] == task:
                return (self.live_dir or live_mailbox.live_dir()) / f"{row['worker']}.sock"
        msg = f"task {task} is not running on any live worker on this host"
        raise LiveOfflineError(msg)

    @staticmethod
    def _call(worker: Path, request: LiveRequest, *, timeout: float) -> object:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        with connection:
            connection.settimeout(_CONNECT_SECONDS)
            try:
                connection.connect(str(worker))
                connection.sendall(json.dumps(request).encode() + b"\n")
            except ConnectionRefusedError as exc:
                worker.unlink(missing_ok=True)
                msg = "nothing listens on it; removed"
                raise _UnreachableError(msg) from exc
            except OSError as exc:
                raise _UnreachableError(exc.strerror or type(exc).__name__) from exc
            connection.settimeout(timeout)
            try:
                raw = connection.makefile("rb").readline(_MAX_RESPONSE_BYTES + 1)
            except OSError as exc:
                msg = f"the answer was lost: {exc.strerror or type(exc).__name__}"
                raise _AnswerLostError(msg) from exc
        try:
            response = json.loads(raw) if raw.endswith(b"\n") else None
        except json.JSONDecodeError:
            response = None
        if not isinstance(response, dict) or not ({"result", "error"} & response.keys()):
            msg = "the connection closed before a valid answer arrived"
            raise _AnswerLostError(msg)
        if "error" in response:
            raise LiveRefusedError(str(response["error"]), response.get("code"))
        return response["result"]
