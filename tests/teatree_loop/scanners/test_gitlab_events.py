"""GitLabEventsScanner: the consumer side of the GitLab group-webhook relay (#271).

Only HTTP is faked (``tests/teatree_backends/_fake_pubsub.py``); the client, the scanner, the
store and the downstream drain run for real against the test database.
"""

import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from django.db import OperationalError
from django.test import TestCase

import teatree.core.overlay_loader as overlay_loader_mod
from teatree.config import get_effective_settings
from teatree.core.gates.merge_guard import MergeGuard
from teatree.core.models import ConfigSetting, IncomingEvent
from teatree.core.views._webhook_persistence import persist_incoming_event
from teatree.loop.scanners.base import ScanSignal
from teatree.loop.scanners.gitlab_events import MAX_PULLS_PER_TICK, PULL_BATCH, GitLabEventsScanner
from teatree.loop.scanners.incoming_events import IncomingEventsScanner
from teatree.loops.inbox.loop import MINI_LOOP as INBOX_LOOP
from teatree.types import ScannerError, ScannerErrorClass
from tests.teatree_backends._fake_pubsub import SUBSCRIPTION, FakePubsub

PERSIST = "teatree.loop.scanners.gitlab_events.persist_incoming_event"


def _merge_request(*, iid: int = 7, title: str = "Add the thing", action: str = "open") -> dict[str, Any]:
    return {
        "object_kind": "merge_request",
        "user": {"username": "alice"},
        "project": {"path_with_namespace": "group/project"},
        "object_attributes": {"iid": iid, "title": title, "action": action},
    }


def _failing_store_for(key: str):
    def persist(record: Any) -> bool:
        if record.idempotency_key == key:
            message = "database is locked"
            raise OperationalError(message)
        return persist_incoming_event(record)

    return persist


class _MrUrlOnlyReview:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def can_auto_merge(self, *, target_ref: str, thread_ref: str) -> MergeGuard:
        self.calls.append((target_ref, thread_ref))
        if target_ref.startswith("https://"):
            return MergeGuard.allow()
        return MergeGuard(allowed=False, escalate=True, reason=f"{target_ref} is not a merge request URL")


class GitLabEventsTestCase(TestCase):
    def setUp(self) -> None:
        self.pubsub = FakePubsub()
        self.enterContext(self.pubsub.installed())
        self.scanner = GitLabEventsScanner(subscription=SUBSCRIPTION)


class TestStoringMessages(GitLabEventsTestCase):
    def test_stores_each_message_keyed_on_its_webhook_id_and_acknowledges_it(self) -> None:
        first = self.pubsub.publish_gitlab(_merge_request(iid=7), webhook_id="wh-1")
        second = self.pubsub.publish_gitlab(_merge_request(iid=8), webhook_id="wh-2")

        assert self.scanner.scan() == []

        event = IncomingEvent.objects.get(idempotency_key="gitlab:wh-1")
        assert (event.source, event.actor, event.channel_ref, event.thread_ref) == (
            IncomingEvent.Source.GITLAB,
            "alice",
            "group/project",
            "7",
        )
        assert event.payload_json == _merge_request(iid=7)
        assert event.processed_at is not None
        assert IncomingEvent.objects.filter(idempotency_key="gitlab:wh-2").exists()
        assert self.pubsub.acked_message_ids == [first, second]

    def test_a_redelivered_webhook_id_is_one_row_and_both_deliveries_are_acknowledged(self) -> None:
        self.pubsub.publish_gitlab(_merge_request(), webhook_id="wh-1")
        self.pubsub.publish_gitlab(_merge_request(), webhook_id="wh-1")
        self.pubsub.publish_gitlab(_merge_request(), webhook_id="wh-2")

        self.scanner.scan()

        assert IncomingEvent.objects.count() == 2
        assert len(self.pubsub.acked_message_ids) == 3

    def test_a_message_acknowledged_late_is_not_stored_twice_when_redelivered(self) -> None:
        self.pubsub.publish_gitlab(_merge_request(), webhook_id="wh-1")
        self.pubsub.ack_status = 503
        with pytest.raises(ScannerError):
            self.scanner.scan()
        assert IncomingEvent.objects.count() == 1

        self.pubsub.ack_status = 200
        self.pubsub.expire_leases()
        self.scanner.scan()

        assert IncomingEvent.objects.count() == 1
        assert len(self.pubsub.acked_message_ids) == 1

    def test_the_body_text_is_the_object_kind_never_a_title_the_text_classifier_would_act_on(self) -> None:
        title = "How do we ship the urgent fix? please review https://gitlab.example.com/g/p/-/merge_requests/9"
        self.pubsub.publish_gitlab(_merge_request(title=title), webhook_id="wh-1")

        self.scanner.scan()

        signals = IncomingEventsScanner().scan()

        assert IncomingEvent.objects.get().body == "merge_request"
        assert [s.kind for s in signals] == []


class TestStoreOnlyShadow(GitLabEventsTestCase):
    def _drain_asking_an_overlay_that_refuses_a_project_path(self) -> tuple[list[ScanSignal], _MrUrlOnlyReview]:
        review = _MrUrlOnlyReview()
        with patch.object(overlay_loader_mod, "get_overlay", return_value=SimpleNamespace(review=review)):
            return IncomingEventsScanner().scan(), review

    def test_an_approval_is_stored_settled_so_the_overlay_merge_guard_is_never_asked(self) -> None:
        self.pubsub.publish_gitlab(_merge_request(action="approved"), webhook_id="wh-1")
        self.scanner.scan()

        signals, review = self._drain_asking_an_overlay_that_refuses_a_project_path()

        assert (signals, review.calls) == ([], [])

    def test_the_same_approval_left_unsettled_escalates_through_that_guard(self) -> None:
        IncomingEvent.objects.create(
            source=IncomingEvent.Source.GITLAB,
            channel_ref="group/project",
            thread_ref="7",
            payload_json=_merge_request(action="approved"),
            idempotency_key="gitlab:unsettled",
        )

        signals, review = self._drain_asking_an_overlay_that_refuses_a_project_path()

        assert review.calls == [("group/project", "7")]
        assert [s.kind for s in signals] == ["incoming_event.merge_escalation"]

    def test_pipeline_events_neither_signal_nor_use_up_the_drain_budget(self) -> None:
        for number in range(30):
            pipeline = {"object_kind": "pipeline", "object_attributes": {"id": number, "status": "success"}}
            self.pubsub.publish_gitlab(pipeline, webhook_id=f"wh-{number}", event="Pipeline Hook")
        self.scanner.scan()
        waiting = IncomingEvent.objects.create(
            source=IncomingEvent.Source.CI,
            body="pipeline succeeded",
            payload_json={"status": "success"},
            idempotency_key="ci:waiting",
        )

        signals = IncomingEventsScanner(limit=25).scan()

        waiting.refresh_from_db()
        assert waiting.processed_at is not None
        assert [s.kind for s in signals] == ["incoming_event.recorded"]


class TestAcknowledgingOnlyAfterTheStore(GitLabEventsTestCase):
    def test_a_message_whose_store_failed_is_left_unacknowledged_and_the_failure_is_loud(self) -> None:
        first = self.pubsub.publish_gitlab(_merge_request(iid=1), webhook_id="wh-1")
        second = self.pubsub.publish_gitlab(_merge_request(iid=2), webhook_id="wh-2")
        third = self.pubsub.publish_gitlab(_merge_request(iid=3), webhook_id="wh-3")

        with patch(PERSIST, side_effect=_failing_store_for("gitlab:wh-2")), pytest.raises(ScannerError) as raised:
            self.scanner.scan()

        assert raised.value.detail == "store failed for gitlab:wh-2 (OperationalError), not acknowledged"
        assert self.pubsub.acked_message_ids == [first, third]
        assert [message["messageId"] for message in self.pubsub.leased.values()] == [second]
        assert set(IncomingEvent.objects.values_list("idempotency_key", flat=True)) == {"gitlab:wh-1", "gitlab:wh-3"}

    def test_the_unacknowledged_message_is_stored_once_it_is_redelivered(self) -> None:
        second = self.pubsub.publish_gitlab(_merge_request(), webhook_id="wh-2")
        with patch(PERSIST, side_effect=_failing_store_for("gitlab:wh-2")), pytest.raises(ScannerError):
            self.scanner.scan()

        self.pubsub.expire_leases()
        self.scanner.scan()

        assert IncomingEvent.objects.filter(idempotency_key="gitlab:wh-2").exists()
        assert self.pubsub.acked_message_ids == [second]


class TestFailingLoudly(GitLabEventsTestCase):
    def test_a_failed_pull_raises_classified_instead_of_reading_as_an_empty_queue(self) -> None:
        expected = {
            503: ScannerErrorClass.UNKNOWN,
            401: ScannerErrorClass.AUTH,
            403: ScannerErrorClass.MISSING_SCOPE,
            429: ScannerErrorClass.RATE_LIMIT,
        }
        for status, error_class in expected.items():
            with self.subTest(status=status):
                self.pubsub.pull_status = status

                with pytest.raises(ScannerError) as raised:
                    self.scanner.scan()

                assert raised.value.error_class is error_class
                assert raised.value.detail == f"pull failed ({status})"

    def test_an_unreachable_pubsub_is_a_network_error(self) -> None:
        self.pubsub.unreachable_hosts.add("pubsub.googleapis.com")

        with pytest.raises(ScannerError) as raised:
            self.scanner.scan()

        assert raised.value.error_class is ScannerErrorClass.NETWORK
        assert raised.value.detail == "pull failed (ConnectError)"

    def test_a_missing_metadata_token_raises_and_nothing_is_pulled(self) -> None:
        self.pubsub.token_status = 404

        with pytest.raises(ScannerError) as raised:
            self.scanner.scan()

        assert raised.value.detail == "no access token from the metadata server (404)"
        assert self.pubsub.pull_bodies == []

    def test_an_unreachable_metadata_server_is_a_network_error(self) -> None:
        self.pubsub.unreachable_hosts.add("metadata.google.internal")

        with pytest.raises(ScannerError) as raised:
            self.scanner.scan()

        assert raised.value.error_class is ScannerErrorClass.NETWORK
        assert raised.value.detail == "no access token from the metadata server (ConnectError)"

    def test_a_failed_acknowledge_raises_after_the_store(self) -> None:
        self.pubsub.publish_gitlab(_merge_request(), webhook_id="wh-1")
        self.pubsub.ack_status = 503

        with pytest.raises(ScannerError) as raised:
            self.scanner.scan()

        assert raised.value.detail == "acknowledge failed (503)"
        assert IncomingEvent.objects.filter(idempotency_key="gitlab:wh-1").exists()


class TestPoisonAndOversizeMessages(GitLabEventsTestCase):
    def test_malformed_messages_become_dead_lettered_rows_and_are_acknowledged(self) -> None:
        no_id = self.pubsub.publish(b'{"object_kind": "push"}', {"X-Gitlab-Event": "Push Hook"})
        not_json = self.pubsub.publish(b"<html>gateway error</html>", {"webhook-id": "wh-2"})
        not_an_object = self.pubsub.publish(b"[1, 2]", {"webhook-id": "wh-3"})

        with pytest.raises(ScannerError):
            self.scanner.scan()

        dead = {event.idempotency_key: event for event in IncomingEvent.objects.dead_lettered()}
        assert set(dead) == {f"gitlab:pubsub:{no_id}", f"gitlab:pubsub:{not_json}", f"gitlab:pubsub:{not_an_object}"}
        assert dead[f"gitlab:pubsub:{no_id}"].last_error == "message without webhook-id"
        assert dead[f"gitlab:pubsub:{not_json}"].last_error == "message data is not JSON"
        assert dead[f"gitlab:pubsub:{not_an_object}"].last_error == "message data is not a JSON object"
        assert all(event.processed_at is None for event in dead.values())
        assert not IncomingEvent.objects.unprocessed().exists()
        assert self.pubsub.acked_message_ids == [no_id, not_json, not_an_object]
        assert self.pubsub.queue == []

    def test_a_dead_lettered_row_keeps_the_attributes_and_the_first_2_kb_of_the_data(self) -> None:
        data = b"<html>" + b"x" * 5000
        attributes = {"webhook-id": "wh-9", "X-Gitlab-Event": "Push Hook"}
        message = self.pubsub.publish(data, attributes)
        self.pubsub.publish_gitlab(_merge_request(), webhook_id="wh-1")

        self.scanner.scan()

        dead = IncomingEvent.objects.get(idempotency_key=f"gitlab:pubsub:{message}")
        assert dead.payload_json == {
            "attributes": attributes,
            "data": data[:2048].decode(),
            "publish_time": "2026-10-01T10:00:00Z",
        }

    def test_a_pull_where_every_message_is_malformed_raises_once_they_are_stored_and_acknowledged(self) -> None:
        first = self.pubsub.publish(b"nope", {"webhook-id": "wh-1"})
        second = self.pubsub.publish(b"[]", {"webhook-id": "wh-2"})

        with pytest.raises(ScannerError) as raised:
            self.scanner.scan()

        assert raised.value.detail == "all 2 messages of a pull were dead-lettered (relay contract changed?)"
        assert IncomingEvent.objects.dead_lettered().count() == 2
        assert self.pubsub.acked_message_ids == [first, second]

    def test_a_body_omitted_message_is_recorded_without_a_body_and_acknowledged(self) -> None:
        message = self.pubsub.publish(
            b"",
            {"webhook-id": "wh-big", "X-Gitlab-Event": "Pipeline Hook", "body-omitted": "true"},
        )

        self.scanner.scan()

        event = IncomingEvent.objects.get(idempotency_key="gitlab:wh-big")
        assert event.payload_json == {
            "body_omitted": True,
            "gitlab_event": "Pipeline Hook",
            "publish_time": "2026-10-01T10:00:00Z",
        }
        assert event.dead_lettered_at is None
        assert event.processed_at is not None
        assert self.pubsub.acked_message_ids == [message]
        assert IncomingEventsScanner().scan() == []


class TestBoundedPulls(GitLabEventsTestCase):
    def test_one_tick_handles_at_most_a_fixed_number_of_pulls_and_leaves_the_rest_queued(self) -> None:
        for number in range(1000):
            self.pubsub.publish_gitlab(_merge_request(iid=number), webhook_id=f"wh-{number}")

        self.scanner.scan()

        assert (PULL_BATCH, MAX_PULLS_PER_TICK) == (50, 5)
        assert IncomingEvent.objects.count() == 250
        assert len(self.pubsub.pull_bodies) == 5
        assert len(self.pubsub.queue) == 750

    def test_an_empty_queue_is_one_pull_and_no_acknowledge(self) -> None:
        self.scanner.scan()

        assert len(self.pubsub.pull_bodies) == 1
        assert self.pubsub.ack_requests == []

    def test_the_tick_ends_on_the_first_empty_pull(self) -> None:
        for number in range(3):
            self.pubsub.publish_gitlab(_merge_request(), webhook_id=f"wh-{number}")

        self.scanner.scan()

        assert len(self.pubsub.pull_bodies) == 2

    def test_a_short_answer_with_backlog_behind_it_is_followed_by_another_pull(self) -> None:
        self.pubsub.max_per_pull = 2
        for number in range(5):
            self.pubsub.publish_gitlab(_merge_request(), webhook_id=f"wh-{number}")

        self.scanner.scan()

        assert IncomingEvent.objects.count() == 5
        assert len(self.pubsub.pull_bodies) == 4


class TestWhatTheScannerNeverWrites(GitLabEventsTestCase):
    def test_logs_never_carry_message_text_or_the_access_token(self) -> None:
        canary = "CANARY-7f3a"
        self.pubsub.publish_gitlab(_merge_request(title=canary), webhook_id="wh-1")
        self.pubsub.publish(canary.encode(), {"webhook-id": "wh-2"})
        self.pubsub.publish_gitlab(_merge_request(), webhook_id="wh-3")

        store_fails = patch(PERSIST, side_effect=_failing_store_for("gitlab:wh-3"))
        with self.assertLogs(level=logging.DEBUG) as logs, store_fails, pytest.raises(ScannerError):
            self.scanner.scan()

        written = "\n".join(logs.output)
        assert canary not in written
        assert "ya29" not in written


class TestInboxWiring(TestCase):
    def _gitlab_events_jobs(self) -> list[Any]:
        return [job for job in INBOX_LOOP.build_jobs() if job.scanner.name == "gitlab_events"]

    def test_there_is_no_job_while_the_subscription_setting_is_empty(self) -> None:
        assert get_effective_settings().gitlab_events_subscription == ""
        assert self._gitlab_events_jobs() == []

    def test_the_job_is_a_global_one_once_the_setting_names_a_subscription(self) -> None:
        ConfigSetting.objects.set_value("gitlab_events_subscription", SUBSCRIPTION)

        (job,) = self._gitlab_events_jobs()

        assert job.overlay == ""
        assert job.scanner.subscription == SUBSCRIPTION

    def test_clearing_the_setting_turns_the_scanner_off_again(self) -> None:
        ConfigSetting.objects.set_value("gitlab_events_subscription", SUBSCRIPTION)
        ConfigSetting.objects.set_value("gitlab_events_subscription", "")

        assert self._gitlab_events_jobs() == []
