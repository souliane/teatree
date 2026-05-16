"""Behaviour tests for the GitLab webhook receiver (#654 phase 6)."""

import json
from unittest.mock import patch

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

    def test_token_is_compared_with_constant_time_primitive(self) -> None:
        """The shared-secret compare must go through ``hmac.compare_digest``.

        A plain ``!=`` short-circuits on the first differing byte, leaking
        the token via response timing. The sibling GitHub/Slack views use
        ``hmac.compare_digest``; this view must too. Asserting the primitive
        is actually invoked (not just that a wrong token 401s — ``!=`` also
        does that) is the anti-vacuous regression for the timing fix.
        """
        body = json.dumps({"object_kind": "merge_request"}).encode()

        with patch(
            "teatree.core.views.gitlab_webhook.hmac.compare_digest",
            wraps=__import__("hmac").compare_digest,
        ) as spy:
            response = _post(self.client, body, token="wrong-token")

        assert response.status_code == 401
        spy.assert_called_once_with("wrong-token", SHARED_SECRET)

    def test_correct_token_via_constant_time_path_is_accepted(self) -> None:
        """The constant-time path must still accept the correct token."""
        payload = {
            "object_kind": "merge_request",
            "user": {"username": "bob"},
            "project": {"path_with_namespace": "org/repo"},
            "object_attributes": {"iid": 7, "action": "open"},
        }
        body = json.dumps(payload).encode()

        with patch(
            "teatree.core.views.gitlab_webhook.hmac.compare_digest",
            wraps=__import__("hmac").compare_digest,
        ) as spy:
            response = _post(self.client, body)

        assert response.status_code == 200
        spy.assert_called_once_with(SHARED_SECRET, SHARED_SECRET)
        assert IncomingEvent.objects.count() == 1

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
