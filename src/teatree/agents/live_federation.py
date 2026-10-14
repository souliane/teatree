"""Bounded broker-to-broker requests over the shared live socket directory."""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import secrets
import stat
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from typing import NoReturn, Protocol, TypedDict, cast

from teatree.agents.live_control import PeerIdentity, RejectCode
from teatree.agents.live_delivery import MailboxRequest, PeerDelivery, SendReceipt, refused_receipt, send_fields
from teatree.agents.live_ingress import IngressRefusedError, PeerRole, PeerSocket, classify_peer, peer_credentials
from teatree.paths import control_db_dir

_MAX_RESPONSE_BYTES = 2_000_000
_CONNECT_SECONDS = 2.0
_MAX_SOCKET_PATH_BYTES = 100
_MAX_WORKERS = 32
_OWNER_MODE = 0o600
_MAX_OWNER_BYTES = 256
_PROOF_FIELDS = ("action", "source_socket", "source_address", "to", "text", "key")
MAX_RELAYED_KEYS = 10_000
logger = logging.getLogger(__name__)
_published: set[Path] = set()


class VerifiedIdentity(TypedDict):
    pid: int
    sender: PeerIdentity


def live_dir() -> Path:
    return control_db_dir(os.environ) / "live"


def publish_path(directory: Path) -> Path:
    path = directory / f"w-{secrets.token_hex(4)}.sock"
    if len(os.fsencode(str(path))) > _MAX_SOCKET_PATH_BYTES:
        msg = f"Live socket path {path} exceeds {_MAX_SOCKET_PATH_BYTES} bytes"
        raise RuntimeError(msg)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    return publish_path(directory) if path.exists() else path


class FederationUnavailableError(Exception):
    pass


class WorkerGoneError(FederationUnavailableError):
    """Nothing listens on the socket: its worker exited, so it holds no peers."""


class FederationRefusedError(Exception):
    pass


def workers(directory: Path, own_socket: Path) -> list[Path]:
    return [path for path in sorted(directory.glob("w-*.sock")) if path != own_socket]


def _process_start(pid: int) -> str:
    try:
        return (Path("/proc") / str(pid) / "stat").read_text().rpartition(") ")[2].split()[19]
    except (OSError, IndexError):
        return ""


class WorkerSocketOwner:
    @staticmethod
    def path(socket_path: Path) -> Path:
        return socket_path.with_suffix(".owner")

    @staticmethod
    def publish(socket_path: Path) -> None:
        details = socket_path.stat()
        payload = {"pid": os.getpid(), "start": _process_start(os.getpid()), "inode": details.st_ino}
        descriptor = os.open(WorkerSocketOwner.path(socket_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, _OWNER_MODE)
        with os.fdopen(descriptor, "w", encoding="ascii") as stream:
            json.dump(payload, stream)
        _published.add(socket_path)

    @staticmethod
    def retire(socket_path: Path) -> None:
        _published.discard(socket_path)
        WorkerSocketOwner.path(socket_path).unlink(missing_ok=True)


def _worker_process(pid: int) -> bool:
    try:
        arguments = (Path("/proc") / str(pid) / "cmdline").read_bytes().split(b"\0")
    except OSError:
        return False
    return any(
        Path(os.fsdecode(argument)).name == "t3" and arguments[index + 1] == b"worker"
        for index, argument in enumerate(arguments[:-1])
    )


def _trusted_worker(socket_path: Path, sock: PeerSocket | None) -> bool:
    credentials = peer_credentials(sock)
    if credentials is None or credentials.pid <= 0 or credentials.uid != os.getuid():
        return False
    try:
        socket_info = socket_path.lstat()
        owner_info = WorkerSocketOwner.path(socket_path).lstat()
        if (
            not stat.S_ISSOCK(socket_info.st_mode)
            or not stat.S_ISREG(owner_info.st_mode)
            or socket_info.st_uid != credentials.uid
            or owner_info.st_uid != credentials.uid
        ):
            return False
        if (
            stat.S_IMODE(socket_info.st_mode) != _OWNER_MODE
            or stat.S_IMODE(owner_info.st_mode) != _OWNER_MODE
            or owner_info.st_size > _MAX_OWNER_BYTES
        ):
            return False
        owner = json.loads(WorkerSocketOwner.path(socket_path).read_text(encoding="ascii"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    return (
        isinstance(owner, dict)
        and owner.get("pid") == credentials.pid
        and owner.get("inode") == socket_info.st_ino
        and bool(owner.get("start"))
        and owner["start"] == _process_start(credentials.pid)
        and (socket_path in _published if credentials.pid == os.getpid() else _worker_process(credentials.pid))
    )


def _require_trusted_worker(socket_path: Path, sock: PeerSocket | None) -> None:
    if not _trusted_worker(socket_path, sock):
        msg = f"{socket_path.name}: worker identity could not be verified"
        raise FederationUnavailableError(msg)


async def remote_peers(
    own_socket: Path, sender: str, secret: bytes, *, strict: bool = False
) -> list[tuple[Path, PeerIdentity]]:
    found: list[tuple[Path, PeerIdentity]] = []
    paths = workers(own_socket.parent, own_socket)
    request = {
        "method": "relay.peers",
        "action": "peers",
        "source_socket": str(own_socket),
        "source_address": sender,
        "to": "",
        "text": "",
        "key": "",
    }
    request["proof"] = proof(secret, request)
    limiter = asyncio.Semaphore(_MAX_WORKERS)

    async def listed(worker: Path) -> object:
        async with limiter:
            return await _listed(worker, request)

    responses = await asyncio.gather(*(listed(worker) for worker in paths), return_exceptions=True)
    live_count = 0
    for worker, response in zip(paths, responses, strict=True):
        if response is None:
            continue
        if isinstance(response, (FederationUnavailableError, FederationRefusedError)):
            logger.warning("Live peer discovery skipped %s: %s", worker.name, response)
            if strict:
                msg = "Peer discovery incomplete"
                raise FederationUnavailableError(msg) from response
            continue
        live_count += 1
        if live_count > _MAX_WORKERS:
            msg = "Live worker directory exceeds the discovery limit"
            raise FederationUnavailableError(msg)
        if not isinstance(response, list) or any(
            not isinstance(peer, dict) or not isinstance(peer.get("address"), str) for peer in response
        ):
            msg = "Peer worker returned invalid discovery data"
            raise FederationUnavailableError(msg)
        found.extend((worker, cast("PeerIdentity", peer)) for peer in response)
    return found


async def _listed(worker: Path, request: Mapping[str, object]) -> object:
    try:
        return await call(worker, request, wait=2)
    except WorkerGoneError:
        return None


def proof(secret: bytes, request: Mapping[str, object]) -> str:
    fields = {name: request.get(name) for name in _PROOF_FIELDS}
    if any(not isinstance(value, str) for value in fields.values()):
        msg = "Peer relay proof needs string fields"
        raise FederationRefusedError(msg)
    return hmac.new(secret, json.dumps(fields, sort_keys=True).encode(), hashlib.sha256).hexdigest()


def valid_proof(secret: bytes, request: Mapping[str, object]) -> bool:
    supplied = request.get("proof")
    return isinstance(supplied, str) and hmac.compare_digest(supplied, proof(secret, request))


async def send_remote(own_socket: Path, sender: str, message: MailboxRequest, secret: bytes) -> SendReceipt:
    to, body, key, wait = send_fields(message)
    for worker, peer in await remote_peers(own_socket, sender, secret, strict=True):
        if peer["address"] != to:
            continue
        request = {
            "method": "relay.send",
            "action": "send",
            "source_socket": str(own_socket),
            "source_address": sender,
            "to": to,
            "text": body,
            "key": key,
            "timeout_seconds": wait,
        }
        request["proof"] = proof(secret, request)
        return cast("SendReceipt", await call(worker, request, wait=wait + 2))
    return refused_receipt(to, RejectCode.SESSION_CLOSED)


async def verified_sender(request: Mapping[str, object], *, own_socket: Path, connected_pid: int) -> PeerIdentity:
    source, address = request.get("source_socket"), request.get("source_address")
    if not isinstance(source, str) or not isinstance(address, str):
        msg = "Peer relay needs a source identity"
        raise FederationRefusedError(msg)
    source_path = Path(source)
    if source_path.parent != own_socket.parent or source_path == own_socket:
        msg = "Peer relay source is outside the live directory"
        raise FederationRefusedError(msg)
    verified = await call(source_path, {**request, "method": "relay.verify"}, wait=2)
    if not isinstance(verified, dict) or (connected_pid != 0 and verified.get("pid") != connected_pid):
        msg = "Peer relay source could not be verified"
        raise FederationRefusedError(msg)
    sender = verified.get("sender")
    if (
        not isinstance(sender, dict)
        or sender.get("address") != address
        or any(not isinstance(sender.get(field), str) for field in ("ticket", "harness", "label"))
    ):
        msg = "Peer relay sender is not active"
        raise FederationRefusedError(msg)
    return cast("PeerIdentity", sender)


async def call(socket_path: Path, request: Mapping[str, object], *, wait: float) -> object:
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(str(socket_path), limit=_MAX_RESPONSE_BYTES), _CONNECT_SECONDS
        )
    except ConnectionRefusedError as exc:
        await asyncio.to_thread(socket_path.unlink, missing_ok=True)
        await asyncio.to_thread(WorkerSocketOwner.retire, socket_path)
        msg = f"{socket_path.name}: nothing listens on it; removed"
        raise WorkerGoneError(msg) from exc
    except FileNotFoundError as exc:
        msg = f"{socket_path.name}: removed by its worker"
        raise WorkerGoneError(msg) from exc
    except (OSError, TimeoutError) as exc:
        msg = f"{socket_path.name}: connection failed"
        raise FederationUnavailableError(msg) from exc
    try:
        _require_trusted_worker(socket_path, writer.get_extra_info("socket"))
        writer.write(json.dumps(request).encode() + b"\n")
        await asyncio.wait_for(writer.drain(), _CONNECT_SECONDS)
        raw = await asyncio.wait_for(reader.readline(), wait)
    except (OSError, TimeoutError) as exc:
        msg = f"{socket_path.name}: answer unavailable"
        raise FederationUnavailableError(msg) from exc
    finally:
        writer.close()
        with suppress(OSError, ConnectionError):
            await writer.wait_closed()
    if not raw.endswith(b"\n") or len(raw) > _MAX_RESPONSE_BYTES:
        msg = f"{socket_path.name}: invalid answer"
        raise FederationUnavailableError(msg)
    try:
        response = json.loads(raw)
    except json.JSONDecodeError as exc:
        msg = f"{socket_path.name}: invalid JSON"
        raise FederationUnavailableError(msg) from exc
    if not isinstance(response, dict):
        msg = f"{socket_path.name}: invalid envelope"
        raise FederationUnavailableError(msg)
    if "error" in response:
        raise FederationRefusedError(str(response["error"]))
    if "result" not in response:
        msg = f"{socket_path.name}: missing result"
        raise FederationUnavailableError(msg)
    return response["result"]


class RelayedBroker(Protocol):
    @property
    def socket_path(self) -> Path: ...

    def identity_of(self, address: str) -> PeerIdentity | None: ...

    def identities(self) -> list[PeerIdentity]: ...

    def enqueue_from(self, sender: PeerIdentity, to: str, body: str) -> PeerDelivery: ...

    async def accepted(self, delivery: PeerDelivery, wait: float) -> SendReceipt: ...


def _refuse(message: str) -> NoReturn:
    raise ValueError(message)


class PeerRelay:
    """Serve sibling workers' ``relay.*`` requests: verify the source broker, then deliver once per sender key."""

    def __init__(self, broker: RelayedBroker) -> None:
        self.secret = secrets.token_bytes(32)
        self._broker = broker
        self._relayed: dict[tuple[str, str], PeerDelivery] = {}

    async def serve(self, method: str, request: MailboxRequest, sock: PeerSocket | None) -> object:
        credentials = peer_credentials(sock)
        if (
            credentials is None
            or classify_peer(credentials, worker_pid=os.getpid(), own_uid=os.getuid()) is not PeerRole.OPERATOR
        ):
            raise IngressRefusedError(RejectCode.PERMISSION_DENIED, "Peer relay requires a worker process")
        if method == "relay.verify":
            return self._verified(request)
        if method not in {"relay.peers", "relay.send"}:
            _refuse("Unknown peer relay method")
        if request.get("action") != method.removeprefix("relay."):
            _refuse("Peer relay action does not match its proof")
        sender = await verified_sender(request, own_socket=self._broker.socket_path, connected_pid=credentials.pid)
        if method == "relay.peers":
            return self._broker.identities()
        return await self._send(sender, request)

    def clear(self) -> None:
        self._relayed.clear()

    def forget(self, recipient: str) -> list[PeerDelivery]:
        forgotten = [delivery for delivery in self._relayed.values() if delivery.to == recipient]
        self._relayed = {entry: delivery for entry, delivery in self._relayed.items() if delivery.to != recipient}
        return forgotten

    async def _send(self, sender: PeerIdentity, request: MailboxRequest) -> SendReceipt:
        to, body, key, wait = send_fields(request)
        entry = (sender["address"], key)
        delivery = self._relayed.get(entry)
        if delivery is None:
            if len(self._relayed) >= MAX_RELAYED_KEYS:
                _refuse("Peer relay is full")
            delivery = self._broker.enqueue_from(sender, to, body)
            self._relayed[entry] = delivery
        elif (delivery.to, delivery.text) != (to, body):
            _refuse("Mailbox key already used for another message")
        return await self._broker.accepted(delivery, wait)

    def _verified(self, request: MailboxRequest) -> VerifiedIdentity:
        address = request.get("source_address")
        sender = self._broker.identity_of(address) if isinstance(address, str) else None
        if (
            sender is None
            or request.get("source_socket") != str(self._broker.socket_path)
            or not valid_proof(self.secret, request)
        ):
            _refuse("Peer relay source could not be verified")
        return {"pid": os.getpid(), "sender": sender}
