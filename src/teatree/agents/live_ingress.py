"""Operator verbs on the worker's live socket, authorized by the kernel's view of the caller.

The worker's own descendants are its agents and their tools, so they are refused; the worker
itself, a process outside its tree, or one in another pid namespace is an operator. A process
that deliberately double-forks out of the worker's tree passes — the same trust boundary every
``t3`` command an agent can run already has; the container is the boundary.
"""

import os
import socket
import struct
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from teatree.agents.live_control import DEFAULT_STEER_WAIT_SECONDS, MAX_STEER_WAIT_SECONDS, RejectCode, SessionFacts
from teatree.agents.live_registry import LiveSessionRegistry, shared_registry

PROC_ROOT = Path("/proc")
_MAX_ANCESTRY_HOPS = 256
_MAX_COMMAND_ID_BYTES = 128


class PeerRole(StrEnum):
    OPERATOR = "operator"
    AGENT = "agent"
    REFUSED = "refused"


@dataclass(frozen=True, slots=True)
class PeerCredentials:
    pid: int
    uid: int


class IngressRefusedError(Exception):
    def __init__(self, code: RejectCode, message: str) -> None:
        super().__init__(message)
        self.code = code


class PeerSocket(Protocol):
    def getsockopt(self, level: int, optname: int, buflen: int, /) -> bytes: ...


def peer_credentials(sock: PeerSocket | None) -> PeerCredentials | None:
    """The connected peer's pid and uid, or ``None`` where the platform cannot say."""
    option = getattr(socket, "SO_PEERCRED", None)  # Linux-only constant
    if sock is None or option is None:
        return None
    try:
        pid, uid, _gid = struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, option, struct.calcsize("3i")))
    except OSError:
        return None
    return PeerCredentials(pid=pid, uid=uid)


def _parent_pid(pid: int, proc_root: Path) -> int | None:
    try:
        status = (proc_root / str(pid) / "status").read_text(encoding="utf-8")
    except OSError:
        return None
    for line in status.splitlines():
        if line.startswith("PPid:"):
            return int(line.split(":", 1)[1])
    return None


def classify_peer(
    credentials: PeerCredentials | None,
    *,
    worker_pid: int,
    own_uid: int,
    proc_root: Path = PROC_ROOT,
) -> PeerRole:
    if credentials is None or credentials.uid != own_uid:
        return PeerRole.REFUSED
    if credentials.pid in {worker_pid, 0}:
        return PeerRole.OPERATOR
    current = credentials.pid
    for _ in range(_MAX_ANCESTRY_HOPS):
        parent = _parent_pid(current, proc_root)
        if parent is None:
            return PeerRole.REFUSED
        if parent == worker_pid:
            return PeerRole.AGENT
        if parent == 0:
            return PeerRole.OPERATOR
        current = parent
    return PeerRole.REFUSED


def _task_pk(request: Mapping[str, object]) -> int:
    task = request.get("task")
    if not isinstance(task, int) or isinstance(task, bool) or task <= 0:
        msg = "Live request needs a positive integer task"
        raise ValueError(msg)
    return task


class OperatorIngress:
    """``live.list`` / ``live.inspect`` (passive) and ``live.steer`` (active) for operators only."""

    def __init__(
        self,
        registry: Callable[[], LiveSessionRegistry] = shared_registry,
        *,
        worker_pid: int | None = None,
    ) -> None:
        self._registry = registry
        self._worker_pid = worker_pid if worker_pid is not None else os.getpid()

    async def serve(self, method: str, request: Mapping[str, object], sock: PeerSocket | None) -> object:
        role = classify_peer(peer_credentials(sock), worker_pid=self._worker_pid, own_uid=os.getuid())
        if role is not PeerRole.OPERATOR:
            raise IngressRefusedError(RejectCode.PERMISSION_DENIED, f"Operator verbs are refused to {role.value} peers")
        if method == "live.list":
            return self._registry().sessions()
        if method == "live.inspect":
            return self._inspect(_task_pk(request))
        if method == "live.steer":
            return await self._steer(request)
        msg = "Unknown live method"
        raise ValueError(msg)

    def _inspect(self, task_pk: int) -> SessionFacts:
        facts = self._registry().facts(task_pk)
        if facts is None:
            raise IngressRefusedError(RejectCode.UNKNOWN_SESSION, f"Task {task_pk} has no live session on this worker")
        return facts

    async def _steer(self, request: Mapping[str, object]) -> object:
        task_pk = _task_pk(request)
        text, command_id = request.get("text"), request.get("command_id")
        wait = request.get("wait_seconds", DEFAULT_STEER_WAIT_SECONDS)
        if not isinstance(text, str) or not text:
            msg = "Live steer needs non-empty text"
            raise ValueError(msg)
        if not isinstance(command_id, str) or not 0 < len(command_id.encode()) <= _MAX_COMMAND_ID_BYTES:
            msg = "Live steer needs a 1-128 byte command_id"
            raise ValueError(msg)
        if not isinstance(wait, (int, float)) or isinstance(wait, bool) or not 0 < wait <= MAX_STEER_WAIT_SECONDS:
            msg = f"Live steer wait must be within (0, {MAX_STEER_WAIT_SECONDS:.0f}] seconds"
            raise ValueError(msg)
        receipt = await self._registry().steer(task_pk, text, command_id=command_id, wait=float(wait))
        return receipt.as_payload()
