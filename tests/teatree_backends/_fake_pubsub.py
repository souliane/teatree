"""An in-memory Pub/Sub pull subscription plus metadata server — the ONLY thing these tests fake is HTTP.

Everything above the wire (the client, the scanner, the store) runs for real. The double is
strict where Google is strict, so a client that forgets the metadata header, the bearer token
or a valid ack id fails here rather than in production: an unknown ack id answers 400, a
message that is pulled and not acknowledged is leased and only comes back through
:meth:`FakePubsub.expire_leases`, the way the ack deadline returns it.
"""

import base64
import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest import mock

import httpx

SUBSCRIPTION = "projects/test-project/subscriptions/gitlab-events"
ACCESS_TOKEN = "ya29.fake-metadata-token"


class FakePubsub:
    def __init__(self) -> None:
        self.queue: list[dict[str, Any]] = []
        self.leased: dict[str, dict[str, Any]] = {}
        self.pull_bodies: list[dict[str, Any]] = []
        self.ack_requests: list[list[str]] = []
        self.acked_message_ids: list[str] = []
        self.token_requests = 0
        self.max_per_pull: int | None = None
        self.token_answer: bytes | None = None
        self.pull_answer: bytes | None = None
        self.token_status = 200
        self.pull_status = 200
        self.ack_status = 200
        self.unreachable_hosts: set[str] = set()
        self._messages = 0
        self._ack_ids = 0

    def publish(self, data: bytes, attributes: dict[str, str]) -> str:
        self._messages += 1
        message_id = f"msg-{self._messages:04d}"
        message = {
            "messageId": message_id,
            "publishTime": "2026-10-01T10:00:00Z",
            "attributes": attributes,
        }
        if data:
            message["data"] = base64.b64encode(data).decode()
        self.queue.append(message)
        return message_id

    def publish_gitlab(self, payload: dict[str, Any], *, webhook_id: str, event: str = "Merge Request Hook") -> str:
        attributes = {"webhook-id": webhook_id, "X-Gitlab-Event": event}
        return self.publish(json.dumps(payload).encode(), attributes)

    def expire_leases(self) -> None:
        self.queue = [*self.leased.values(), *self.queue]
        self.leased = {}

    @property
    def acked_ack_ids(self) -> list[str]:
        return [ack_id for request in self.ack_requests for ack_id in request]

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.host in self.unreachable_hosts:
            message = "connection refused"
            raise httpx.ConnectError(message, request=request)
        if request.url.host == "metadata.google.internal":
            return self._token(request)
        if request.url.host == "pubsub.googleapis.com":
            return self._api(request)
        return httpx.Response(404)

    def _token(self, request: httpx.Request) -> httpx.Response:
        self.token_requests += 1
        if request.headers.get("Metadata-Flavor") != "Google":
            return httpx.Response(403)
        if self.token_status != httpx.codes.OK:
            return httpx.Response(self.token_status)
        if self.token_answer is not None:
            return httpx.Response(200, content=self.token_answer)
        return httpx.Response(200, json={"access_token": ACCESS_TOKEN, "expires_in": 3599, "token_type": "Bearer"})

    def _api(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("Authorization") != f"Bearer {ACCESS_TOKEN}":
            return httpx.Response(401, json={"error": {"status": "UNAUTHENTICATED"}})
        body = json.loads(request.content)
        if request.url.path == f"/v1/{SUBSCRIPTION}:pull":
            return self._pull(body)
        if request.url.path == f"/v1/{SUBSCRIPTION}:acknowledge":
            return self._acknowledge(body)
        return httpx.Response(404, json={"error": {"status": "NOT_FOUND"}})

    def _pull(self, body: dict[str, Any]) -> httpx.Response:
        self.pull_bodies.append(body)
        if self.pull_status != httpx.codes.OK:
            return httpx.Response(self.pull_status, json={"error": {"status": "UNAVAILABLE"}})
        if self.pull_answer is not None:
            return httpx.Response(200, content=self.pull_answer)
        size = min(body["maxMessages"], self.max_per_pull or body["maxMessages"])
        batch, self.queue = self.queue[:size], self.queue[size:]
        received = []
        for message in batch:
            self._ack_ids += 1
            ack_id = f"ack-{self._ack_ids:05d}"
            self.leased[ack_id] = message
            received.append({"ackId": ack_id, "message": message})
        return httpx.Response(200, json={"receivedMessages": received} if received else {})

    def _acknowledge(self, body: dict[str, Any]) -> httpx.Response:
        if self.ack_status != httpx.codes.OK:
            return httpx.Response(self.ack_status, json={"error": {"status": "UNAVAILABLE"}})
        ack_ids = body["ackIds"]
        if not ack_ids or any(ack_id not in self.leased for ack_id in ack_ids):
            return httpx.Response(400, json={"error": {"status": "INVALID_ARGUMENT"}})
        self.ack_requests.append(ack_ids)
        self.acked_message_ids.extend(self.leased.pop(ack_id)["messageId"] for ack_id in ack_ids)
        return httpx.Response(200, json={})

    @contextmanager
    def installed(self) -> Iterator["FakePubsub"]:
        """Point every ``httpx.Client`` in the process at this double, with a millisecond retry backoff."""
        original = httpx.Client.__init__

        def patched(client: httpx.Client, **kwargs: Any) -> None:
            kwargs["transport"] = httpx.MockTransport(self.handler)
            original(client, **kwargs)

        with (
            mock.patch.object(httpx.Client, "__init__", patched),
            mock.patch.dict("os.environ", {"T3_PUBSUB_HTTP_BACKOFF": "0.001"}),
        ):
            yield self
