"""Envelope-shaped outage classification consulted by the recorder chokepoint (#1764)."""

import pytest

from teatree.agents.outage_classifier import outage_signature


@pytest.mark.parametrize(
    "text",
    [
        "Unable to connect to API",
        "ConnectionRefused while dispatching",
        "FailedToOpenSocket",
        "safety classifier unavailable",
    ],
)
def test_connection_signature_in_summary_is_outage(text: str) -> None:
    assert outage_signature({"summary": text})


@pytest.mark.parametrize(
    "text",
    [
        "UNABLE TO CONNECT TO API",
        "connectionrefused",
        "FAILEDTOOPENSOCKET",
        "SAFETY CLASSIFIER UNAVAILABLE",
    ],
)
def test_signature_match_is_case_insensitive(text: str) -> None:
    assert outage_signature({"summary": text})


def test_signature_in_user_input_reason_is_outage() -> None:
    assert outage_signature({"user_input_reason": "Unable to connect to API"})


def test_signature_in_error_arg_is_outage() -> None:
    assert outage_signature({"summary": "ok"}, error="connection refused")


def test_api_error_with_connection_cooccurrence_is_outage() -> None:
    assert outage_signature({"summary": "API Error: connection reset by peer"})


def test_api_error_alone_is_not_outage() -> None:
    assert outage_signature({"summary": "Added API Error handling and retries"}) == ""


def test_legit_completion_is_not_outage() -> None:
    assert outage_signature({"summary": "Implemented the recover command and tests"}) == ""


def test_empty_result_is_not_outage() -> None:
    assert outage_signature({}) == ""


def test_summary_and_error_are_scanned_together() -> None:
    # The "API Error" phrase and its co-occurring connection word can land in
    # different envelope fields; the flattened haystack must still match.
    assert outage_signature({"summary": "API Error"}, error="socket") == "api error + socket"
