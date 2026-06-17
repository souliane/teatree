"""Behaviour tests for the Slack webhook receiver (#654 phase 1)."""

import hashlib
import hmac
import json
import time

from django.test import Client, TestCase, override_settings
from django.urls import reverse

from teatree.core.models import IncomingEvent

SIGNING_SECRET = "test-signing-secret"


def _sign(body: bytes, timestamp: str, *, secret: str = SIGNING_SECRET) -> str:
    basestring = b"v0:" + timestamp.encode() + b":" + body
    digest = hmac.new(secret.encode(), basestring, hashlib.sha256).hexdigest()
    return f"v0={digest}"


def _post(client: Client, body: bytes, *, timestamp: str | None = None, signature: str | None = None):
    timestamp = timestamp if timestamp is not None else str(int(time.time()))
    signature = signature if signature is not None else _sign(body, timestamp)
    return client.post(
        reverse("teatree:slack_webhook"),
        data=body,
        content_type="application/json",
        HTTP_X_SLACK_REQUEST_TIMESTAMP=timestamp,
        HTTP_X_SLACK_SIGNATURE=signature,
    )


@override_settings(TEATREE_SLACK_SIGNING_SECRET=SIGNING_SECRET)
class TestSlackWebhookView(TestCase):
    def test_url_verification_challenge_is_echoed(self) -> None:
        body = json.dumps({"type": "url_verification", "challenge": "abc123xyz"}).encode()

        response = _post(self.client, body)

        assert response.status_code == 200
        assert response.json() == {"challenge": "abc123xyz"}
        assert IncomingEvent.objects.count() == 0

    def test_event_callback_persists_incoming_event(self) -> None:
        payload = {
            "type": "event_callback",
            "event_id": "Ev0PV52K21",
            "event": {
                "type": "app_mention",
                "user": "U02ABCDEF",
                "text": "<@U0LAN0Z89> can you review !42?",
                "channel": "C024BE91L",
                "ts": "1234567890.000200",
                "thread_ts": "1234567890.000100",
            },
        }
        body = json.dumps(payload).encode()

        response = _post(self.client, body)

        assert response.status_code == 200
        event = IncomingEvent.objects.get()
        assert event.source == IncomingEvent.Source.SLACK
        assert event.actor == "U02ABCDEF"
        assert event.channel_ref == "C024BE91L"
        assert event.thread_ref == "1234567890.000100"
        assert "review !42" in event.body
        assert event.idempotency_key == "slack:Ev0PV52K21"
        assert event.payload_json["event"]["type"] == "app_mention"

    def test_thread_reply_persists_parent_ts(self) -> None:
        payload = {
            "type": "event_callback",
            "event_id": "Ev0THREADRPLY",
            "event": {
                "type": "message",
                "user": "U02ABCDEF",
                "text": "where is the URL?",
                "channel": "C024BE91L",
                "ts": "1234567890.000200",
                "thread_ts": "1234567890.000100",
            },
        }
        body = json.dumps(payload).encode()

        response = _post(self.client, body)

        assert response.status_code == 200
        event = IncomingEvent.objects.get()
        assert event.parent_ts == "1234567890.000100"
        assert event.is_thread_reply is True

    def test_thread_root_message_has_no_parent_ts(self) -> None:
        payload = {
            "type": "event_callback",
            "event_id": "Ev0ROOTMSG",
            "event": {
                "type": "message",
                "user": "U02ABCDEF",
                "text": "approve posting the evidence?",
                "channel": "C024BE91L",
                "ts": "1234567890.000100",
                "thread_ts": "1234567890.000100",
            },
        }
        body = json.dumps(payload).encode()

        response = _post(self.client, body)

        assert response.status_code == 200
        event = IncomingEvent.objects.get()
        assert event.parent_ts == ""
        assert event.is_thread_reply is False

    def test_replays_are_idempotent(self) -> None:
        payload = {"type": "event_callback", "event_id": "Ev0DUPLICATE", "event": {"type": "message"}}
        body = json.dumps(payload).encode()

        _post(self.client, body)
        response = _post(self.client, body)

        assert response.status_code == 200
        assert IncomingEvent.objects.filter(idempotency_key="slack:Ev0DUPLICATE").count() == 1

    def test_rejects_request_with_bad_signature(self) -> None:
        body = json.dumps({"type": "event_callback", "event_id": "Ev1"}).encode()
        timestamp = str(int(time.time()))

        response = _post(self.client, body, timestamp=timestamp, signature="v0=deadbeef")

        assert response.status_code == 401
        assert IncomingEvent.objects.count() == 0

    def test_rejects_stale_timestamp(self) -> None:
        body = json.dumps({"type": "event_callback", "event_id": "Ev1"}).encode()
        stale_ts = str(int(time.time()) - 60 * 10)

        response = _post(self.client, body, timestamp=stale_ts)

        assert response.status_code == 401
        assert IncomingEvent.objects.count() == 0

    def test_rejects_missing_signature_headers(self) -> None:
        body = json.dumps({"type": "event_callback", "event_id": "Ev1"}).encode()

        response = self.client.post(
            reverse("teatree:slack_webhook"),
            data=body,
            content_type="application/json",
        )

        assert response.status_code == 401
        assert IncomingEvent.objects.count() == 0

    def test_missing_event_id_fallback_key_is_content_hash(self) -> None:
        payload = {"type": "event_callback", "event": {"type": "message", "text": "hello"}}
        body = json.dumps(payload).encode()

        response = _post(self.client, body)

        assert response.status_code == 200
        event = IncomingEvent.objects.get()
        expected_hash = hashlib.sha256(body).hexdigest()[:16]
        assert event.idempotency_key == f"slack:{expected_hash}"

    def test_missing_event_id_retries_collapse_to_one_row(self) -> None:
        payload = {"type": "event_callback", "event": {"type": "message", "text": "hello"}}
        body = json.dumps(payload).encode()

        _post(self.client, body)
        _post(self.client, body)

        assert IncomingEvent.objects.count() == 1


@override_settings(TEATREE_SLACK_SIGNING_SECRET="")
class TestSlackWebhookViewWithoutSecret(TestCase):
    def test_returns_503_when_not_configured(self) -> None:
        body = json.dumps({"type": "url_verification", "challenge": "x"}).encode()

        response = self.client.post(
            reverse("teatree:slack_webhook"),
            data=body,
            content_type="application/json",
            headers={"x-slack-request-timestamp": "1", "x-slack-signature": "v0=x"},
        )

        assert response.status_code == 503
