"""PubsubSubscription: pull and acknowledge over REST with the VM's metadata-server token."""

import json
from collections.abc import Iterator
from typing import Any

import pytest

from teatree.backends.pubsub import PubsubSubscription
from teatree.types import ScannerError, ScannerErrorClass
from tests.teatree_backends._fake_pubsub import SUBSCRIPTION, FakePubsub


@pytest.fixture
def pubsub() -> Iterator[FakePubsub]:
    with FakePubsub().installed() as fake:
        yield fake


@pytest.fixture
def subscription() -> PubsubSubscription:
    return PubsubSubscription(SUBSCRIPTION, scanner="gitlab_events")


def test_pull_decodes_the_message_and_asks_for_an_immediate_answer(
    pubsub: FakePubsub, subscription: PubsubSubscription
) -> None:
    message_id = pubsub.publish(b'{"object_kind": "push"}', {"webhook-id": "wh-1"})

    (message,) = subscription.pull(10)

    assert message.data == b'{"object_kind": "push"}'
    assert message.attributes == {"webhook-id": "wh-1"}
    assert (message.message_id, message.publish_time) == (message_id, "2026-10-01T10:00:00Z")
    assert message.ack_id.startswith("ack-")
    assert pubsub.pull_bodies == [{"maxMessages": 10, "returnImmediately": True}]


def test_a_message_without_data_decodes_to_empty_bytes(pubsub: FakePubsub, subscription: PubsubSubscription) -> None:
    pubsub.publish(b"", {"webhook-id": "wh-1", "body-omitted": "true"})

    (message,) = subscription.pull(10)

    assert message.data == b""


def test_an_empty_subscription_pulls_nothing(pubsub: FakePubsub, subscription: PubsubSubscription) -> None:
    assert subscription.pull(10) == []


def test_the_token_is_fetched_once_for_the_pull_and_the_acknowledge(
    pubsub: FakePubsub, subscription: PubsubSubscription
) -> None:
    pubsub.publish(b"{}", {"webhook-id": "wh-1"})

    (message,) = subscription.pull(10)
    subscription.acknowledge([message.ack_id])

    assert pubsub.token_requests == 1
    assert pubsub.acked_ack_ids == [message.ack_id]


def test_acknowledging_nothing_sends_nothing(pubsub: FakePubsub, subscription: PubsubSubscription) -> None:
    subscription.acknowledge([])

    assert pubsub.token_requests == 0
    assert pubsub.ack_requests == []


def test_an_acknowledge_google_rejects_raises(pubsub: FakePubsub, subscription: PubsubSubscription) -> None:
    with pytest.raises(ScannerError) as raised:
        subscription.acknowledge(["ack-unknown"])

    assert raised.value.detail == "acknowledge failed (400)"


def test_a_subscription_path_the_project_does_not_have_raises(pubsub: FakePubsub) -> None:
    missing = PubsubSubscription("projects/test-project/subscriptions/other", scanner="gitlab_events")

    with pytest.raises(ScannerError) as raised:
        missing.pull(10)

    assert raised.value.detail == "pull failed (404)"


@pytest.mark.parametrize(
    ("answer", "reason"),
    [(b"<html>gateway</html>", "answer is not JSON"), (b"[]", "answer is not a JSON object")],
)
def test_a_pull_answer_that_is_not_a_json_object_raises_classified(
    pubsub: FakePubsub, subscription: PubsubSubscription, answer: bytes, reason: str
) -> None:
    pubsub.pull_answer = answer

    with pytest.raises(ScannerError) as raised:
        subscription.pull(10)

    assert raised.value.error_class is ScannerErrorClass.UNKNOWN
    assert raised.value.detail == f"pull failed ({reason})"


@pytest.mark.parametrize(
    "received",
    [
        "payload-secret",
        ["payload-secret"],
        [{"message": {"messageId": "m-1"}}],
        [{"ackId": 7, "message": {"messageId": "m-1"}}],
        [{"ackId": "ack-1"}],
        [{"ackId": "ack-1", "message": "payload-secret"}],
        [{"ackId": "ack-1", "message": {"messageId": "m-1", "data": "payload-secret"}}],
        [{"ackId": "ack-1", "message": {"messageId": "m-1", "data": 7}}],
        [{"ackId": "ack-1", "message": {}}],
    ],
)
def test_malformed_pull_envelopes_raise_a_fixed_classified_error(
    pubsub: FakePubsub, subscription: PubsubSubscription, received: Any
) -> None:
    pubsub.pull_answer = json.dumps({"receivedMessages": received}).encode()

    with pytest.raises(ScannerError) as raised:
        subscription.pull(10)

    assert raised.value.error_class is ScannerErrorClass.UNKNOWN
    assert raised.value.detail == "pull failed (malformed receivedMessages)"
    assert "payload-secret" not in str(raised.value)


@pytest.mark.parametrize(
    ("answer", "reason"),
    [
        (b"<html>gateway</html>", "answer is not JSON"),
        (b"[]", "answer is not a JSON object"),
        (b'{"expires_in": 3599}', "answer has no access_token"),
    ],
)
def test_a_token_answer_without_a_token_raises_classified(
    pubsub: FakePubsub, subscription: PubsubSubscription, answer: bytes, reason: str
) -> None:
    pubsub.token_answer = answer

    with pytest.raises(ScannerError) as raised:
        subscription.pull(10)

    assert raised.value.error_class is ScannerErrorClass.UNKNOWN
    assert raised.value.detail == f"no access token from the metadata server ({reason})"
    assert pubsub.pull_bodies == []
