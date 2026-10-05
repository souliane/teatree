"""Pub/Sub pull subscription over REST, authenticated with the VM's metadata-server token.

No Google library and no stored secret: the identity is the one the VM already carries, so
the subscription path is the only thing an operator configures. Every failure raises
:class:`~teatree.types.ScannerError` — a failed pull must never read as an empty queue.
Retries ride the shared bounded transport (knobs ``T3_PUBSUB_HTTP_*``).
"""

import base64
import binascii
from collections.abc import Callable
from dataclasses import dataclass
from typing import cast

import httpx

from teatree.backends.http_retry import SimpleRetryTransport
from teatree.types import RawAPIDict, ScannerError, ScannerErrorClass

_METADATA_URL = "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"
_API = "https://pubsub.googleapis.com/v1"
_TIMEOUT_SECONDS = 10.0

_CLASS_BY_STATUS = {
    httpx.codes.UNAUTHORIZED: ScannerErrorClass.AUTH,
    httpx.codes.FORBIDDEN: ScannerErrorClass.MISSING_SCOPE,
    httpx.codes.TOO_MANY_REQUESTS: ScannerErrorClass.RATE_LIMIT,
}


@dataclass(frozen=True, slots=True)
class PubsubMessage:
    ack_id: str
    message_id: str
    publish_time: str
    data: bytes
    attributes: dict[str, str]


class PubsubSubscription:
    def __init__(self, path: str, *, scanner: str) -> None:
        self.path = path
        self._scanner = scanner
        self._transport = SimpleRetryTransport(env_prefix="T3_PUBSUB_HTTP")
        self._access_token = ""

    def pull(self, max_messages: int) -> list[PubsubMessage]:
        # returnImmediately: an empty subscription must not park the tick until Pub/Sub answers.
        body = self._post("pull", {"maxMessages": max_messages, "returnImmediately": True})
        received = body.get("receivedMessages", [])
        failure = "pull failed"
        reason = "malformed receivedMessages"
        if not isinstance(received, list):
            raise self._error(failure, reason, ScannerErrorClass.UNKNOWN)
        try:
            return [_message(entry) for entry in received]
        except (TypeError, ValueError, binascii.Error) as exc:
            raise self._error(failure, reason, ScannerErrorClass.UNKNOWN) from exc

    def acknowledge(self, ack_ids: list[str]) -> None:
        if ack_ids:
            self._post("acknowledge", {"ackIds": ack_ids})

    def _post(self, method: str, payload: RawAPIDict) -> RawAPIDict:
        with httpx.Client(timeout=_TIMEOUT_SECONDS) as client:
            headers = {"Authorization": f"Bearer {self._token(client)}"}
            url = f"{_API}/{self.path}:{method}"
            response = self._send(f"{method} failed", lambda: client.post(url, json=payload, headers=headers))
        return self._json_object(f"{method} failed", response)

    def _token(self, client: httpx.Client) -> str:
        if not self._access_token:
            failure = "no access token from the metadata server"
            response = self._send(failure, lambda: client.get(_METADATA_URL, headers={"Metadata-Flavor": "Google"}))
            token = self._json_object(failure, response).get("access_token")
            if not isinstance(token, str) or not token:
                raise self._error(failure, "answer has no access_token", ScannerErrorClass.UNKNOWN)
            self._access_token = token
        return self._access_token

    def _send(self, failure: str, attempt: Callable[[], httpx.Response]) -> httpx.Response:
        try:
            response = self._transport.run(attempt, idempotent=True)
        except httpx.TransportError as exc:
            raise self._error(failure, type(exc).__name__, ScannerErrorClass.NETWORK) from exc
        if not response.is_success:
            error_class = _CLASS_BY_STATUS.get(response.status_code, ScannerErrorClass.UNKNOWN)
            raise self._error(failure, str(response.status_code), error_class)
        return response

    def _json_object(self, failure: str, response: httpx.Response) -> RawAPIDict:
        try:
            body = response.json()
        except ValueError as exc:
            raise self._error(failure, "answer is not JSON", ScannerErrorClass.UNKNOWN) from exc
        if not isinstance(body, dict):
            raise self._error(failure, "answer is not a JSON object", ScannerErrorClass.UNKNOWN)
        return cast("RawAPIDict", body)

    def _error(self, failure: str, reason: str, error_class: ScannerErrorClass) -> ScannerError:
        return ScannerError(scanner=self._scanner, error_class=error_class, detail=f"{failure} ({reason})")


def _message(received: object) -> PubsubMessage:
    if not isinstance(received, dict):
        raise TypeError
    ack_id = received.get("ackId")
    message = received.get("message")
    if not isinstance(ack_id, str) or not isinstance(message, dict):
        raise TypeError
    if not ack_id:
        raise ValueError
    message_id = message.get("messageId")
    publish_time = message.get("publishTime", "")
    data = message.get("data", "")
    attributes = message.get("attributes", {})
    if not isinstance(message_id, str) or not isinstance(publish_time, str) or not isinstance(data, str):
        raise TypeError
    if not isinstance(attributes, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in attributes.items()
    ):
        raise TypeError
    if not message_id:
        raise ValueError
    return PubsubMessage(
        ack_id=ack_id,
        message_id=message_id,
        publish_time=publish_time,
        data=base64.b64decode(data, validate=True),
        attributes=attributes,
    )
