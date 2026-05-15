"""Behaviour tests for the GitLab webhook receiver (#654 phase 6)."""

import json

from django.test import Client, TestCase, override_settings
from django.urls import reverse

from teatree.core.models import IncomingEvent

SHARED_SECRET = "test-gitlab-secret"


def _post(client: Client, body: bytes, *, token: str | None = SHARED_SECRET, event: str = "Merge Request Hook"):
    headers: dict[str, str] = {"HTTP_X_GITLAB_EVENT": event}
    if token is not None:
        headers["HTTP_X_GITLAB_TOKEN"] = token
    return client.post(
        reverse("teatree:gitlab_webhook"),
        data=body,
        content_type="application/json",
        **headers,
    )


@override_settings(TEATREE_GITLAB_WEBHOOK_TOKEN=SHARED_SECRET)
class TestGitLabWebhookView(TestCase):
    def test_merge_request_approved_event_is_persisted(self) -> None:
        payload = {
            "object_kind": "merge_request",
            "user": {"username": "alice"},
            "project": {"path_with_namespace": "org/repo"},
            "object_attributes": {"iid": 42, "action": "approved", "url": "https://gl/x/y/-/merge_requests/42"},
        }
        body = json.dumps(payload).encode()

        response = _post(self.client, body)

        assert response.status_code == 200
        event = IncomingEvent.objects.get()
        assert event.source == IncomingEvent.Source.GITLAB
        assert event.actor == "alice"
        assert event.channel_ref == "org/repo"
        assert event.idempotency_key.startswith("gitlab:")
        assert event.payload_json["object_attributes"]["action"] == "approved"

    def test_rejects_request_with_wrong_token(self) -> None:
        body = json.dumps({"object_kind": "merge_request"}).encode()

        response = _post(self.client, body, token="not-the-secret")

        assert response.status_code == 401
        assert IncomingEvent.objects.count() == 0

    def test_rejects_missing_token(self) -> None:
        body = json.dumps({"object_kind": "merge_request"}).encode()

        response = _post(self.client, body, token=None)

        assert response.status_code == 401

    def test_replays_are_idempotent(self) -> None:
        payload = {
            "object_kind": "merge_request",
            "user": {"username": "alice"},
            "project": {"path_with_namespace": "org/repo"},
            "object_attributes": {"iid": 42, "action": "open"},
        }
        body = json.dumps(payload).encode()

        _post(self.client, body)
        response = _post(self.client, body)

        assert response.status_code == 200
        assert IncomingEvent.objects.count() == 1


@override_settings(TEATREE_GITLAB_WEBHOOK_TOKEN="")
class TestGitLabWebhookViewWithoutSecret(TestCase):
    def test_returns_503_when_not_configured(self) -> None:
        response = _post(self.client, b"{}", token="anything")

        assert response.status_code == 503
