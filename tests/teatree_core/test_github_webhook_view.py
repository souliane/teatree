"""Behaviour tests for the GitHub webhook receiver (#654 phase 6)."""

import hashlib
import hmac
import json

from django.test import Client, TestCase, override_settings
from django.urls import reverse

from teatree.core.models import IncomingEvent

SECRET = "test-github-secret"


def _sign(body: bytes, *, secret: str = SECRET) -> str:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _post(
    client: Client,
    body: bytes,
    *,
    signature: str | None = None,
    event: str = "pull_request_review",
    delivery: str = "abc-123",
):
    headers: dict[str, str] = {
        "HTTP_X_GITHUB_EVENT": event,
        "HTTP_X_GITHUB_DELIVERY": delivery,
    }
    signature = signature if signature is not None else _sign(body)
    headers["HTTP_X_HUB_SIGNATURE_256"] = signature
    return client.post(
        reverse("teatree:github_webhook"),
        data=body,
        content_type="application/json",
        **headers,
    )


@override_settings(TEATREE_GITHUB_WEBHOOK_SECRET=SECRET)
class TestGitHubWebhookView(TestCase):
    def test_pull_request_review_approved_persists_event(self) -> None:
        payload = {
            "action": "submitted",
            "review": {"state": "approved", "user": {"login": "bob"}},
            "repository": {"full_name": "owner/repo"},
            "pull_request": {"number": 17, "html_url": "https://github.com/owner/repo/pull/17"},
            "sender": {"login": "bob"},
        }
        body = json.dumps(payload).encode()

        response = _post(self.client, body)

        assert response.status_code == 200
        event = IncomingEvent.objects.get()
        assert event.source == IncomingEvent.Source.GITHUB
        assert event.actor == "bob"
        assert event.channel_ref == "owner/repo"
        assert event.idempotency_key == "github:abc-123"
        assert event.payload_json["review"]["state"] == "approved"

    def test_rejects_request_with_bad_signature(self) -> None:
        body = json.dumps({"action": "opened"}).encode()

        response = _post(self.client, body, signature="sha256=deadbeef")

        assert response.status_code == 401
        assert IncomingEvent.objects.count() == 0

    def test_rejects_missing_signature(self) -> None:
        body = json.dumps({"action": "opened"}).encode()
        response = self.client.post(
            reverse("teatree:github_webhook"),
            data=body,
            content_type="application/json",
            headers={"x-github-event": "ping", "x-github-delivery": "ping-1"},
        )

        assert response.status_code == 401

    def test_replays_are_idempotent(self) -> None:
        payload = {"action": "opened", "sender": {"login": "x"}, "repository": {"full_name": "owner/repo"}}
        body = json.dumps(payload).encode()

        _post(self.client, body, delivery="dup-1")
        response = _post(self.client, body, delivery="dup-1")

        assert response.status_code == 200
        assert IncomingEvent.objects.count() == 1


@override_settings(TEATREE_GITHUB_WEBHOOK_SECRET="")
class TestGitHubWebhookViewWithoutSecret(TestCase):
    def test_returns_503_when_not_configured(self) -> None:
        response = self.client.post(
            reverse("teatree:github_webhook"),
            data=b"{}",
            content_type="application/json",
            headers={"x-github-event": "ping", "x-github-delivery": "x", "x-hub-signature-256": "sha256=x"},
        )

        assert response.status_code == 503
