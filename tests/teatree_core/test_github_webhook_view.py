"""Behaviour tests for the GitHub webhook receiver (#654 phase 6, #4795)."""

import hashlib
import hmac
import json

from django.test import Client, TestCase, override_settings
from django.urls import reverse

from teatree.core.models import GitHubAppInstallation, IncomingEvent, WebhookRejection

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
        # No entity identity is derivable from this minimal fixture (no
        # ``pull_request.updated_at``), so the key falls back to the delivery
        # UUID — namespaced ``github:delivery:<id>`` (#4795) rather than the
        # pre-#4795 bare ``github:<id>``, to stay visually distinct from an
        # identity-derived key while preserving the same dedup guarantee.
        assert event.idempotency_key == "github:delivery:abc-123"
        assert event.payload_json["review"]["state"] == "approved"

    def test_rejects_request_with_bad_signature(self) -> None:
        body = json.dumps({"action": "opened"}).encode()

        response = _post(self.client, body, signature="sha256=deadbeef")

        assert response.status_code == 401
        assert IncomingEvent.objects.count() == 0
        rejection = WebhookRejection.objects.get()
        assert rejection.reason == WebhookRejection.Reason.SIGNATURE_INVALID
        assert rejection.source == IncomingEvent.Source.GITHUB

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

    @override_settings(TEATREE_MAX_WEBHOOK_PAYLOAD_BYTES=100)
    def test_oversized_payload_is_rejected_before_persistence(self) -> None:
        payload = {"action": "opened", "sender": {"login": "x"}, "padding": "y" * 200}
        body = json.dumps(payload).encode()
        assert len(body) > 100

        response = _post(self.client, body, event="pull_request", delivery="oversize-1")

        assert response.status_code == 413
        assert IncomingEvent.objects.count() == 0
        rejection = WebhookRejection.objects.get()
        assert rejection.reason == WebhookRejection.Reason.OVERSIZED

    def test_a_stale_replay_is_suppressed_and_never_persisted(self) -> None:
        newer = {
            "repository": {"full_name": "owner/repo"},
            "pull_request": {"number": 17, "title": "t", "updated_at": "2026-09-25T12:00:00Z"},
            "sender": {"login": "x"},
        }
        older = {
            "repository": {"full_name": "owner/repo"},
            "pull_request": {"number": 17, "title": "t", "updated_at": "2026-09-25T10:00:00Z"},
            "sender": {"login": "x"},
        }

        first = _post(self.client, json.dumps(newer).encode(), event="pull_request", delivery="d-newer")
        second = _post(self.client, json.dumps(older).encode(), event="pull_request", delivery="d-older")

        assert first.status_code == 200
        assert second.status_code == 200
        assert IncomingEvent.objects.count() == 1
        assert IncomingEvent.objects.get().payload_json["pull_request"]["updated_at"] == "2026-09-25T12:00:00Z"
        rejection = WebhookRejection.objects.get()
        assert rejection.reason == WebhookRejection.Reason.STALE_REPLAY

    def test_a_newer_update_after_an_older_one_is_accepted(self) -> None:
        older = {
            "repository": {"full_name": "owner/repo"},
            "pull_request": {"number": 17, "title": "t", "updated_at": "2026-09-25T10:00:00Z"},
            "sender": {"login": "x"},
        }
        newer = {
            "repository": {"full_name": "owner/repo"},
            "pull_request": {"number": 17, "title": "t", "updated_at": "2026-09-25T12:00:00Z"},
            "sender": {"login": "x"},
        }

        _post(self.client, json.dumps(older).encode(), event="pull_request", delivery="d-older")
        _post(self.client, json.dumps(newer).encode(), event="pull_request", delivery="d-newer")

        assert IncomingEvent.objects.count() == 2
        assert WebhookRejection.objects.count() == 0

    def test_installation_event_routes_to_installation_sync(self) -> None:
        payload = {
            "action": "created",
            "installation": {
                "id": 555,
                "app_id": 42,
                "account": {"login": "souliane", "type": "Organization"},
                "repository_selection": "selected",
                "permissions": {"pull_requests": "read"},
                "events": ["pull_request"],
            },
            "repositories": [{"full_name": "souliane/teatree"}],
            "sender": {"login": "souliane"},
        }
        response = _post(self.client, json.dumps(payload).encode(), event="installation", delivery="inst-1")

        assert response.status_code == 200
        installation = GitHubAppInstallation.objects.get(installation_id=555)
        assert installation.account_login == "souliane"
        assert installation.pending_repositories == ["souliane/teatree"]
        assert installation.active_repositories == []


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
        rejection = WebhookRejection.objects.get()
        assert rejection.reason == WebhookRejection.Reason.NO_SECRET
