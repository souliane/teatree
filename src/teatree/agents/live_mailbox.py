"""Task-owned live mailbox and operator ingress, federated over workers' Unix sockets."""

import asyncio
import atexit
import json
import logging
import os
import secrets
import threading
import uuid
from collections import deque
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from time import monotonic
from typing import Self

from teatree.agents.live_control import ControlOutcome, LiveTask, PeerIdentity, RejectCode, frame_peer_message
from teatree.agents.live_delivery import (
    _MAX_INBOX_MESSAGES,
    InboxMessage,
    InboxPage,
    MailboxIdentity,
    MailboxRequest,
    MailboxTextTooLargeError,
    PeerDelivery,
    PushOnlyTask,
    SendReceipt,
    _Participant,
    _reject,
    mailbox_wait_timeout,
    peer_identity,
    refused_receipt,
    send_fields,
)
from teatree.agents.live_federation import (
    FederationRefusedError,
    FederationUnavailableError,
    PeerRelay,
    WorkerSocketOwner,
    live_dir,
    publish_path,
    remote_peers,
    send_remote,
)
from teatree.agents.live_ingress import (
    IngressRefusedError,
    OperatorIngress,
    PeerRole,
    PeerSocket,
    classify_peer,
    peer_credentials,
)
from teatree.agents.live_registry import LiveSessionRegistry, shared_registry

logger = logging.getLogger(__name__)
OPERATOR_HARNESS = "interactive"

_MAX_REQUEST_BYTES = 128_000  # JSON escaping can expand a valid 16-KiB body sixfold
_MAX_PAGE_SIZE = 100
_MAX_PAGE_BYTES = 1_800_000
MAX_WAITING_SENDS = 4


class LiveMailboxBroker:
    """Own only sessions currently running in this worker process."""

    def __init__(
        self,
        *,
        runtime_dir: Path | None = None,
        ingress: OperatorIngress | None = None,
        registry: Callable[[], LiveSessionRegistry] = shared_registry,
    ) -> None:
        self._runtime_dir = runtime_dir
        self._registry = registry
        self._ingress = ingress or OperatorIngress(registry)
        self._socket_path: Path | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._startup_error: BaseException | None = None
        self._lock = threading.RLock()
        self._by_token: dict[str, _Participant] = {}
        self._by_address: dict[str, _Participant] = {}
        self._next_message_id = 0
        self._delivery: dict[int, PeerDelivery] = {}
        self._joined_writers: set[asyncio.StreamWriter] = set()
        self._relay = PeerRelay(self)
        self._close_callbacks: list[Callable[[], None]] = []

    def add_close_callback(self, callback: Callable[[], None]) -> None:
        with self._lock:
            self._close_callbacks.append(callback)

    @property
    def socket_path(self) -> Path:
        if self._socket_path is None:
            msg = "Live mailbox broker has not started"
            raise RuntimeError(msg)
        return self._socket_path

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._ready.clear()
        self._startup_error = None
        self._socket_path = publish_path(self._runtime_dir or live_dir())
        self._thread = threading.Thread(target=self._run, name="t3-live-mailbox", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=5):
            msg = "Live mailbox broker did not start"
            raise RuntimeError(msg)
        if self._startup_error is not None:
            self._thread.join(timeout=5)
            self._thread = None
            self._socket_path = None
            msg = "Live mailbox broker failed to start"
            raise RuntimeError(msg) from self._startup_error

    def close(self) -> None:
        with self._lock:
            callbacks, self._close_callbacks = self._close_callbacks, []
        for callback in callbacks:
            callback()
        loop, stop, thread = self._loop, self._stop, self._thread
        if loop is not None and not loop.is_closed() and stop is not None and thread is not None and thread.is_alive():
            loop.call_soon_threadsafe(stop.set)
            thread.join(timeout=5)
        with self._lock:
            self._by_token.clear()
            self._by_address.clear()
            self._delivery.clear()
            self._relay.clear()
        self._thread = None
        self._loop = None
        self._stop = None

    def register(
        self,
        *,
        room: str,
        harness: str,
        label: str,
        task: LiveTask | PushOnlyTask | None = None,
        can_receive: bool = True,
    ) -> MailboxIdentity:
        if (not room and harness != OPERATOR_HARNESS) or not harness or not label:
            msg = "Mailbox room, harness, and label are required"
            raise ValueError(msg)
        self.start()
        push_only = isinstance(task, PushOnlyTask)
        bound_task = task.task if push_only else task
        participant = _Participant(
            address=str(uuid.uuid4()),
            token=secrets.token_urlsafe(32),
            room=room,
            harness=harness,
            label=label,
            phase=bound_task.phase if bound_task else "",
            can_receive=can_receive,
            task=bound_task,
            reads_inbox=not push_only,
            inbox=deque(maxlen=_MAX_INBOX_MESSAGES),
        )
        with self._lock:
            self._by_token[participant.token] = participant
            self._by_address[participant.address] = participant
        return MailboxIdentity(self.socket_path, participant.address, participant.token)

    def unregister(self, address: str) -> None:
        with self._lock:
            participant = self._by_address.pop(address, None)
            if participant is None:
                return
            self._by_token.pop(participant.token, None)
            received = [row["id"] for row in participant.inbox]
            sent = [delivery.message_id for delivery in participant.sent.values()]
        loop = self._loop
        if loop is not None and not loop.is_closed():
            with suppress(RuntimeError):
                loop.call_soon_threadsafe(self._recipient_left, address, received)
                loop.call_soon_threadsafe(self._sender_left, sent)

    def _recipient_left(self, address: str, message_ids: list[int]) -> None:
        for message_id in message_ids:
            if (delivery := self._delivery.get(message_id)) is not None:
                delivery.queued_in_inbox = False
                if delivery.push is not None and not delivery.push.done():
                    delivery.push.add_done_callback(
                        lambda _finished, pending=delivery: pending.settle(
                            ControlOutcome.REJECTED, RejectCode.SESSION_CLOSED
                        )
                    )
                else:
                    delivery.settle(ControlOutcome.REJECTED, RejectCode.SESSION_CLOSED)
        self._sender_left([delivery.message_id for delivery in self._relay.forget(address)])

    def _sender_left(self, message_ids: list[int]) -> None:
        for message_id in message_ids:
            self._delivery.pop(message_id, None)

    def _run(self) -> None:
        try:
            asyncio.run(self._serve())
        except Exception as exc:  # noqa: BLE001 - propagate startup failure to waiting callers
            self._startup_error = exc
            self._ready.set()
        finally:
            if self._socket_path is not None:
                self._socket_path.unlink(missing_ok=True)
                WorkerSocketOwner.retire(self._socket_path)

    async def _serve(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        server = await asyncio.start_unix_server(self._handle, path=str(self.socket_path), limit=_MAX_REQUEST_BYTES)
        self.socket_path.chmod(0o600)
        WorkerSocketOwner.publish(self.socket_path)
        self._ready.set()
        async with server:
            await self._stop.wait()
            server.close()
            writers = tuple(self._joined_writers)
            for writer in writers:
                writer.close()
            await asyncio.gather(*(writer.wait_closed() for writer in writers))
            await server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            try:
                raw = await reader.readline()
            except asyncio.CancelledError:
                writer.close()
                await writer.wait_closed()
                raise
            if not raw:
                writer.close()
                await writer.wait_closed()
                return
            if len(raw) > _MAX_REQUEST_BYTES or not raw.endswith(b"\n"):
                _reject("Mailbox request is too large")
            request = json.loads(raw)
            if not isinstance(request, dict):
                _reject("Invalid mailbox request")
            method = request.get("method")
            if method == "peer.join":
                await self._handle_join(reader, writer)
                return
            result = await self._route_request(request, writer)
            response = {"result": result}
        except IngressRefusedError as exc:
            response = {"error": str(exc), "code": exc.code.value}
        except (
            ValueError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            FederationRefusedError,
            FederationUnavailableError,
        ) as exc:
            response = {"error": str(exc)}
        except Exception:  # noqa: BLE001 - never expose broker internals to an MCP child
            response = {"error": "Mailbox request failed"}
        try:
            writer.write(json.dumps(response).encode() + b"\n")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async def _handle_join(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        identity = self._join(writer.get_extra_info("socket"))
        self._joined_writers.add(writer)
        try:
            payload = {"result": {"address": identity.address, "token": identity.token}}
            writer.write(json.dumps(payload).encode() + b"\n")
            await writer.drain()
            await reader.read()
        finally:
            self._joined_writers.discard(writer)
            self.unregister(identity.address)
            writer.close()
            await writer.wait_closed()

    async def _route_request(self, request: MailboxRequest, writer: asyncio.StreamWriter) -> object:
        method = request.get("method")
        if isinstance(method, str) and method.startswith("live."):
            return await self._ingress.serve(method, request, writer.get_extra_info("socket"))
        if isinstance(method, str) and method.startswith("relay."):
            return await self._relay.serve(method, request, writer.get_extra_info("socket"))
        return await self._dispatch(request)

    def _join(self, sock: PeerSocket | None) -> MailboxIdentity:
        if self._stop is not None and self._stop.is_set():
            raise IngressRefusedError(RejectCode.SESSION_CLOSED, "Live mailbox broker is stopping")
        role = classify_peer(peer_credentials(sock), worker_pid=os.getpid(), own_uid=os.getuid())
        if role is not PeerRole.OPERATOR:
            raise IngressRefusedError(RejectCode.PERMISSION_DENIED, "Interactive mailbox join requires an operator")
        return self.register(room="", harness=OPERATOR_HARNESS, label="attended")

    async def _dispatch(self, request: MailboxRequest) -> object:
        token = request.get("token")
        if not isinstance(token, str):
            _reject("Invalid mailbox identity")
        with self._lock:
            participant = self._by_token.get(token)
        if participant is None:
            _reject("Invalid mailbox identity")
        method = request.get("method")
        if method == "self":
            return self._identity(participant)
        if method == "peers":
            return await self._peers(participant)
        if method == "send":
            return await self._send(participant, request)
        if method == "ack":
            return self._ack(participant, request)
        if method == "inbox":
            return self._inbox(participant, request)
        if method == "wait":
            return await self._wait(participant, request)
        msg = "Unknown mailbox method"
        raise ValueError(msg)

    @staticmethod
    def _identity(participant: _Participant) -> PeerIdentity:
        return peer_identity(participant)

    def identity_of(self, address: str) -> PeerIdentity | None:
        with self._lock:
            participant = self._by_address.get(address)
            return None if participant is None else self._identity(participant)

    def identities(self) -> list[PeerIdentity]:
        with self._lock:
            return [self._identity(peer) for peer in self._by_address.values()]

    def enqueue_from(self, sender: PeerIdentity, to: str, body: str) -> PeerDelivery:
        with self._lock:
            return self._enqueue(sender, to, body)

    async def accepted(self, delivery: PeerDelivery, wait: float) -> SendReceipt:
        with self._lock:
            recipient = self._by_address.get(delivery.to)
        return await self._accepted(delivery, recipient, wait)

    def _require_active(self, participant: _Participant) -> None:
        if self._by_address.get(participant.address) is not participant:
            _reject("Invalid mailbox identity")

    async def _peers(self, participant: _Participant) -> list[PeerIdentity]:
        with self._lock:
            self._require_active(participant)
        local = [peer for peer in self.identities() if peer["address"] != participant.address]
        remote = await remote_peers(self.socket_path, participant.address, self._relay.secret)
        return local + [peer for _, peer in remote]

    async def _send(self, participant: _Participant, request: MailboxRequest) -> SendReceipt:
        try:
            to, body, key, timeout = send_fields(request)
        except MailboxTextTooLargeError:
            to = request.get("to")
            if not isinstance(to, str):
                raise
            return refused_receipt(to, RejectCode.TOO_LARGE)
        with self._lock:
            self._require_active(participant)
            if participant.waiting >= MAX_WAITING_SENDS:
                return refused_receipt(to, RejectCode.BACKPRESSURE)
            delivery = participant.sent.get(key)
            previous = participant.remote_sent.prior(key, to, body)
            if previous is not None:
                return previous
            if delivery is None and participant.remote_sent.is_full_for(key, len(participant.sent)):
                _reject("Mailbox send limit reached for this task")
            if delivery is None and to in self._by_address:
                delivery = self._enqueue(self._identity(participant), to, body)
                participant.sent[key] = delivery
            elif delivery is not None and (delivery.to, delivery.text) != (to, body):
                _reject("Mailbox key already used for another message")
            recipient = self._by_address.get(to)
            participant.waiting += 1
        try:
            if delivery is None:
                receipt = await send_remote(self.socket_path, participant.address, request, self._relay.secret)
                with self._lock:
                    participant.remote_sent.keep(key, to, body, receipt)
                return receipt
            return await self._accepted(delivery, recipient, timeout)
        finally:
            participant.waiting -= 1

    def _enqueue(self, sender: PeerIdentity, to: str, body: str) -> PeerDelivery:
        recipient = self._by_address.get(to)
        if recipient is None:
            _reject("Mailbox recipient is not active")
        self._next_message_id += 1
        message_id = self._next_message_id
        framed = frame_peer_message(body, message_id=message_id, sender=sender)
        delivery = PeerDelivery(message_id, to, body, framed, asyncio.get_running_loop().create_future())
        self._delivery[message_id] = delivery
        if not recipient.can_receive:
            delivery.queued_in_inbox = False
            delivery.settle(ControlOutcome.REJECTED, RejectCode.RECIPIENT_CANNOT_RECEIVE)
            return delivery
        if not recipient.reads_inbox:
            delivery.queued_in_inbox = False
            return delivery
        if len(recipient.inbox) == recipient.inbox.maxlen:
            evicted = recipient.inbox[0]["id"]
            if (unread := self._delivery.get(evicted)) is not None:
                unread.queued_in_inbox = False
                unread.settle(ControlOutcome.REJECTED, RejectCode.DROPPED_UNREAD)
            recipient.dropped_before = evicted
        recipient.inbox.append(
            {
                "id": message_id,
                "from": sender["address"],
                "from_harness": sender["harness"],
                "from_label": sender["label"],
                "text": body,
            }
        )
        return delivery

    async def _accepted(self, delivery: PeerDelivery, recipient: _Participant | None, wait: float) -> SendReceipt:
        """Push into the recipient's running turn while its inbox read can still accept first."""
        deadline = asyncio.get_running_loop().time() + wait
        if not delivery.settled.done():
            was_unknown = delivery.unknown_reported
            if not was_unknown:
                if recipient is not None and recipient.task is not None and delivery.push is None:
                    delivery.push = asyncio.create_task(self._push_into_turn(delivery, recipient.task, wait))
                if delivery.push is not None:
                    await asyncio.wait(
                        {delivery.settled, delivery.push}, timeout=wait, return_when=asyncio.FIRST_COMPLETED
                    )
            if delivery.push_outcome is not ControlOutcome.UNKNOWN_DELIVERY or (
                was_unknown and delivery.queued_in_inbox
            ):
                try:
                    await asyncio.wait_for(
                        asyncio.shield(delivery.settled), max(0, deadline - asyncio.get_running_loop().time())
                    )
                except TimeoutError:
                    if delivery.push is not None and not delivery.push.done():
                        await asyncio.wait({delivery.push})
            delivery.unknown_reported = delivery.push_outcome is ControlOutcome.UNKNOWN_DELIVERY
        return delivery.receipt()

    async def _push_into_turn(self, delivery: PeerDelivery, task: LiveTask, wait: float) -> None:
        command_id = f"peer-{delivery.message_id}"
        receipt = await self._registry().push(
            task.pk, delivery.framed, command_id=command_id, wait=wait, fence=task.fence
        )
        delivery.record_push(receipt)

    def _ack(self, participant: _Participant, request: MailboxRequest) -> bool:
        ids = request.get("ids")
        if (
            not isinstance(ids, list)
            or len(ids) > _MAX_PAGE_SIZE
            or any(
                not isinstance(message_id, int) or isinstance(message_id, bool) or message_id <= 0 for message_id in ids
            )
        ):
            _reject("Mailbox acknowledgement needs up to 100 positive message IDs")
        with self._lock:
            self._require_active(participant)
            read = set(ids)
            for row in participant.inbox:
                if row["id"] in read and (delivery := self._delivery.get(row["id"])) is not None:
                    delivery.settle(ControlOutcome.ACCEPTED_INBOX_READ)
        return True

    def _inbox(self, participant: _Participant, request: MailboxRequest) -> InboxPage:
        after_id, limit = request.get("after_id", 0), request.get("limit", 50)
        if not isinstance(after_id, int) or isinstance(after_id, bool) or after_id < 0:
            _reject("Mailbox cursor must be nonnegative")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= _MAX_PAGE_SIZE:
            _reject("Mailbox page size must be 1-100")
        with self._lock:
            self._require_active(participant)
            messages: list[InboxMessage] = []
            encoded_bytes = 0
            for row in participant.inbox:
                if row["id"] <= after_id:
                    continue
                row_bytes = len(json.dumps(row).encode())
                if messages and encoded_bytes + row_bytes > _MAX_PAGE_BYTES:
                    break
                messages.append(row)
                encoded_bytes += row_bytes
                if len(messages) >= limit:
                    break
            truncated = after_id < participant.dropped_before
        return {
            "messages": messages,
            "next_cursor": messages[-1]["id"] if messages else after_id,
            "truncated": truncated,
        }

    async def _wait(self, participant: _Participant, request: MailboxRequest) -> InboxPage:
        timeout = mailbox_wait_timeout(request)
        deadline = monotonic() + timeout
        while True:
            result = self._inbox(participant, request)
            if result["messages"] or monotonic() >= deadline:
                return result
            await asyncio.sleep(min(0.05, deadline - monotonic()))


_shared_brokers: dict[int, LiveMailboxBroker] = {}
_shared_lock = threading.Lock()


def shared_broker() -> LiveMailboxBroker:
    pid = os.getpid()
    with _shared_lock:
        if pid not in _shared_brokers:
            broker = LiveMailboxBroker()
            broker.start()
            _shared_brokers[pid] = broker
        return _shared_brokers[pid]


def reset_shared_brokers() -> None:
    """Close and forget worker-local brokers between tests or at process exit."""
    with _shared_lock:
        for broker in _shared_brokers.values():
            broker.close()
        _shared_brokers.clear()
        WorkerSocketOwner.reset()


atexit.register(reset_shared_brokers)
