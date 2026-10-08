"""Behaviour tests for the GitHub webhook receiver."""

import hashlib
import hmac
import json
import logging
from unittest.mock import patch

import pytest
from django.http import HttpResponse
from django.test import TestCase, override_settings
from django.urls import reverse

from teatree.core.models import IncomingEvent, Task
from teatree.loop.scanners.incoming_events import IncomingEventsScanner
from teatree.utils import secrets

PREFIX = "teatree/github-webhook"
SECRET_A = "test-secret-alpha"
SECRET_B = "test-secret-bravo"
KEY_A = f"{PREFIX}/repository-101"
KEY_B = f"{PREFIX}/integration-202"
KEY_EMPTY = f"{PREFIX}/repository-303"
TARGET_B = {"X-GitHub-Hook-Installation-Target-Type": "integration", "X-GitHub-Hook-Installation-Target-ID": "202"}
TARGET_EMPTY = {"X-GitHub-Hook-Installation-Target-ID": "303"}
TARGET_UNLISTED = {"X-GitHub-Hook-Installation-Target-ID": "999"}
DEFAULT_BODY = json.dumps({"action": "opened", "sender": {"login": "bob"}}).encode()


def _sign(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _payload(**fields: object) -> bytes:
    return json.dumps({"action": "opened", **fields}).encode()


class FakeStore:
    def __init__(self, entries: dict[str, str]) -> None:
        self.entries = entries
        self.reads: list[str] = []
        self.listings = 0
        self.read_error: Exception | None = None
        self.list_error: Exception | None = None

    def read_pass(self, key: str) -> str:
        self.reads.append(key)
        if self.read_error is not None:
            raise self.read_error
        return self.entries.get(key, "")

    def pass_entry_names(self, prefix: str) -> frozenset[str]:
        self.listings += 1
        if self.list_error is not None:
            raise self.list_error
        return frozenset(key.removeprefix(f"{prefix}/") for key in self.entries if key.startswith(f"{prefix}/"))

    @property
    def touched(self) -> bool:
        return bool(self.reads) or self.listings > 0


class GitHubWebhookTestCase(TestCase):
    @pytest.fixture(autouse=True)
    def _inject_fixtures(self, caplog: pytest.LogCaptureFixture) -> None:
        self.caplog = caplog
        caplog.set_level(logging.DEBUG)

    def setUp(self) -> None:
        self.store = FakeStore({KEY_A: SECRET_A, KEY_B: SECRET_B, KEY_EMPTY: ""})
        self.enterContext(
            patch.multiple(secrets, read_pass=self.store.read_pass, pass_entry_names=self.store.pass_entry_names),
        )

    def deliver(
        self,
        body: bytes = DEFAULT_BODY,
        *,
        secret: str = SECRET_A,
        headers: dict[str, str | None] | None = None,
        **meta: str,
    ) -> HttpResponse:
        sent: dict[str, str | None] = {
            "X-GitHub-Delivery": "d-1",
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Hook-Installation-Target-Type": "repository",
            "X-GitHub-Hook-Installation-Target-ID": "101",
            "X-Hub-Signature-256": _sign(body, secret),
        } | (headers or {})
        return self.client.post(
            reverse("teatree:github_webhook"),
            data=body,
            content_type="application/json",
            headers={name: value for name, value in sent.items() if value is not None},
            **meta,
        )


class TestAcceptedDelivery(GitHubWebhookTestCase):
    def test_signed_delivery_stores_one_row_keyed_by_delivery_with_event_name(self) -> None:
        body = _payload(
            sender={"login": "bob"},
            repository={"full_name": "owner/repo"},
            pull_request={"number": 17, "title": "Tidy"},
        )

        response = self.deliver(body, headers={"X-GitHub-Delivery": "abc-123", "X-GitHub-Event": "pull_request_review"})

        assert response.status_code == 200
        row = IncomingEvent.objects.get()
        assert (row.source, row.idempotency_key, row.event_name) == ("github", "github:abc-123", "pull_request_review")
        assert (row.actor, row.channel_ref, row.thread_ref) == ("bob", "owner/repo", "17")

    def test_row_is_stored_settled_so_the_drain_never_dispatches_it(self) -> None:
        body = _payload(pull_request={"number": 4, "title": "Handle the case where the cache is cold"})

        assert self.deliver(body).status_code == 200

        assert IncomingEvent.objects.get().processed_at is not None
        assert IncomingEventsScanner().scan() == []
        assert not Task.objects.exists()

    def test_redelivery_leaves_one_row(self) -> None:
        statuses = [self.deliver(headers={"X-GitHub-Delivery": "dup-1"}).status_code for _ in range(2)]

        assert statuses == [200, 200]
        assert IncomingEvent.objects.count() == 1

    @override_settings(TEATREE_WEBHOOK_RATE_CAPACITY=60, TEATREE_WEBHOOK_RATE_REFILL_PER_SECOND=0.0)
    def test_signed_burst_is_never_throttled(self) -> None:
        statuses = {self.deliver(headers={"X-GitHub-Delivery": f"burst-{n}"}).status_code for n in range(61)}

        assert statuses == {200}
        assert IncomingEvent.objects.count() == 61

    def test_signature_covers_the_raw_bytes_not_a_reserialisation(self) -> None:
        body = '{"action":"opened" ,  "sender": {"login": "zo\u00eb"}}'.encode()

        assert self.deliver(body).status_code == 200
        assert IncomingEvent.objects.get().actor == "zo\u00eb"

    def test_odd_payload_shapes_are_stored_never_500(self) -> None:
        cases = [
            ("pull_request", {"sender": "x"}),
            ("pull_request_review", {"review": {"user": None}}),
            ("pull_request", {"pull_request": []}),
            ("issue_comment", {"issue": None, "comment": "x"}),
        ]
        for index, (event, fields) in enumerate(cases):
            with self.subTest(event=event, fields=fields):
                headers: dict[str, str | None] = {"X-GitHub-Event": event, "X-GitHub-Delivery": f"odd-{index}"}

                assert self.deliver(_payload(**fields), headers=headers).status_code == 200
        assert IncomingEvent.objects.count() == len(cases)


class TestMalformedRequest(GitHubWebhookTestCase):
    def test_each_header_missing_or_not_fully_matching_is_400_before_the_store(self) -> None:
        malformed = {
            "X-GitHub-Delivery": ("d-1", "d_1"),
            "X-GitHub-Event": ("pull_request", "Pull_Request"),
            "X-GitHub-Hook-Installation-Target-Type": ("repository", "marketplace::listing"),
            "X-GitHub-Hook-Installation-Target-ID": ("101", "10a"),
        }
        for header, (valid, bad) in malformed.items():
            for value in (None, bad, "repository/../admin-password", f"{valid}\n"):
                with self.subTest(header=header, value=value):
                    assert self.deliver(headers={header: value}).status_code == 400
                    assert not self.store.touched
        assert not IncomingEvent.objects.exists()

    def test_signed_body_that_is_not_a_json_object_is_400(self) -> None:
        for body in (b"not json", b"[]", b'"x"', b"\xff"):
            with self.subTest(body=body):
                assert self.deliver(body).status_code == 400
        assert not IncomingEvent.objects.exists()

    def test_content_length_is_checked_before_the_store(self) -> None:
        cases = [
            ("missing", b"", {}, 411),
            ("blank", DEFAULT_BODY, {"CONTENT_LENGTH": ""}, 411),
            ("not a number", DEFAULT_BODY, {"CONTENT_LENGTH": "abc"}, 400),
            ("negative", DEFAULT_BODY, {"CONTENT_LENGTH": "-1"}, 400),
            ("above the cap", DEFAULT_BODY, {"CONTENT_LENGTH": "2621441"}, 413),
        ]
        for label, body, meta, status in cases:
            with self.subTest(label):
                assert self.deliver(body, **meta).status_code == status
                assert not self.store.touched
        assert not IncomingEvent.objects.exists()

    @override_settings(DATA_UPLOAD_MAX_MEMORY_SIZE=None)
    def test_no_cap_means_no_413(self) -> None:
        assert self.deliver().status_code == 200

    def test_content_length_at_the_cap_is_accepted_and_one_above_is_413(self) -> None:
        for cap, status in ((len(DEFAULT_BODY), 200), (len(DEFAULT_BODY) - 1, 413)):
            with self.subTest(cap=cap), override_settings(DATA_UPLOAD_MAX_MEMORY_SIZE=cap):
                assert self.deliver(headers={"X-GitHub-Delivery": f"cap-{cap}"}).status_code == status

    @override_settings(DATA_UPLOAD_MAX_MEMORY_SIZE=16)
    def test_413_warns_with_delivery_event_and_length(self) -> None:
        response = self.deliver(headers={"X-GitHub-Delivery": "big-7", "X-GitHub-Event": "push"})

        assert response.status_code == 413
        warnings = [
            r.getMessage() for r in self.caplog.records if r.levelno == logging.WARNING and "big-7" in r.getMessage()
        ]
        assert len(warnings) == 1
        assert "push" in warnings[0]
        assert str(len(DEFAULT_BODY)) in warnings[0]


class TestSecretSelection(GitHubWebhookTestCase):
    def test_signature_for_another_listed_target_is_401(self) -> None:
        assert self.deliver().status_code == 200

        response = self.deliver(headers={**TARGET_B, "X-GitHub-Delivery": "d-2"})

        assert response.status_code == 401
        assert IncomingEvent.objects.count() == 1

    def test_the_body_cannot_choose_the_secret(self) -> None:
        self.store.entries[f"{PREFIX}/repository-202"] = SECRET_B
        body = _payload(repository={"id": 202, "full_name": "owner/other"})

        assert self.deliver(body, secret=SECRET_B).status_code == 401
        assert self.deliver(body, headers={"X-GitHub-Delivery": "d-2"}).status_code == 200

    def test_missing_or_malformed_signature_is_401_before_the_store(self) -> None:
        for signature in (None, "sha256=deadbeef", "sha1=" + "0" * 40, "sha256=" + "\u00e9" * 64):
            with self.subTest(signature=signature):
                assert self.deliver(headers={"X-Hub-Signature-256": signature}).status_code == 401
                assert not self.store.touched
        assert not IncomingEvent.objects.exists()

    def test_unlisted_target_is_401_without_a_read(self) -> None:
        response = self.deliver(headers=TARGET_UNLISTED)

        assert response.status_code == 401
        assert self.store.reads == []
        assert not IncomingEvent.objects.exists()

    def test_listed_target_reading_back_empty_is_503_even_signed_with_the_empty_key(self) -> None:
        response = self.deliver(secret="", headers=TARGET_EMPTY)

        assert response.status_code == 503
        assert self.store.reads == [KEY_EMPTY]
        assert not IncomingEvent.objects.exists()

    def test_store_failure_is_503_and_warns_with_the_cause(self) -> None:
        for failing, cause in (("list_error", "store dir unreadable"), ("read_error", "keyring did not answer")):
            with self.subTest(failing):
                self.store = FakeStore({KEY_A: SECRET_A})
                setattr(self.store, failing, secrets.SecretStoreError(cause))
                with patch.multiple(
                    secrets,
                    read_pass=self.store.read_pass,
                    pass_entry_names=self.store.pass_entry_names,
                ):
                    assert self.deliver().status_code == 503
                assert any(r.levelno == logging.WARNING and cause in r.getMessage() for r in self.caplog.records)
        assert not IncomingEvent.objects.exists()

    def test_warm_target_is_not_read_again(self) -> None:
        for delivery in ("d-1", "d-2"):
            assert self.deliver(headers={"X-GitHub-Delivery": delivery}).status_code == 200

        assert self.store.reads == [KEY_A]

    def test_rotated_secret_is_picked_up_by_one_refresh_per_window(self) -> None:
        assert self.deliver().status_code == 200
        self.store.entries[KEY_A] = "rotated-secret-1d2e"

        assert self.deliver(secret="rotated-secret-1d2e", headers={"X-GitHub-Delivery": "d-2"}).status_code == 200
        assert self.deliver(secret="wrong-secret", headers={"X-GitHub-Delivery": "d-3"}).status_code == 401

        assert self.store.reads == [KEY_A, KEY_A]

    def test_refresh_reading_back_empty_evicts_and_is_503(self) -> None:
        assert self.deliver().status_code == 200
        self.store.entries[KEY_A] = ""

        assert self.deliver(secret="", headers={"X-GitHub-Delivery": "d-2"}).status_code == 503
        assert self.deliver(headers={"X-GitHub-Delivery": "d-3"}).status_code == 503
        assert IncomingEvent.objects.count() == 1

    def test_sprayed_requests_do_not_starve_a_cold_listed_target(self) -> None:
        for n in range(100):
            self.deliver(headers={"X-GitHub-Hook-Installation-Target-ID": str(5000 + n)})
            self.deliver(secret="forged", headers={"X-GitHub-Delivery": f"forged-{n}"})

        assert self.deliver(secret=SECRET_B, headers={**TARGET_B, "X-GitHub-Delivery": "real-1"}).status_code == 200

    def test_unknown_target_warning_is_bounded(self) -> None:
        for n in range(50):
            assert self.deliver(headers={"X-GitHub-Hook-Installation-Target-ID": str(7000 + n)}).status_code == 401

        warnings = [
            r for r in self.caplog.records if r.levelno == logging.WARNING and r.name.startswith("teatree.core.views")
        ]
        assert len(warnings) == 1


class TestNothingSecretLeaks(GitHubWebhookTestCase):
    def test_secret_signature_and_body_never_reach_logs_or_rows(self) -> None:
        body = _payload(pull_request={"number": 9, "title": "Title"}, marker="canary-body-6f1e")
        paths = [
            ("200", SECRET_A, {}, {}),
            ("400", SECRET_A, {"X-GitHub-Event": "Bad"}, {}),
            ("401", "forged-secret-77", {}, {}),
            ("411", SECRET_A, {}, {"CONTENT_LENGTH": ""}),
            ("413", SECRET_A, {}, {"CONTENT_LENGTH": "2621441"}),
            ("503", "", TARGET_EMPTY, {}),
        ]
        for status, secret, headers, meta in paths:
            with self.subTest(status):
                self.caplog.clear()
                signature = _sign(body, secret)
                delivery = {"X-GitHub-Delivery": f"canary-{status}"}

                assert str(self.deliver(body, secret=secret, headers=headers | delivery, **meta).status_code) == status

                rows = " ".join(
                    str(getattr(row, field.attname))
                    for row in IncomingEvent.objects.all()
                    for field in row._meta.fields
                )
                for needle in (SECRET_A, signature.removeprefix("sha256="), "canary-body-6f1e"):
                    assert needle not in self.caplog.text
                for needle in (SECRET_A, signature.removeprefix("sha256=")):
                    assert needle not in rows
