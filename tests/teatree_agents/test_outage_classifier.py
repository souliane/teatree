"""Pure outage-death classifier consulted by the recorder chokepoint (#1764)."""

import pytest

from teatree.agents.outage_classifier import is_outage_death


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
    assert is_outage_death({"summary": text}) is True


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
    assert is_outage_death({"summary": text}) is True


def test_signature_in_user_input_reason_is_outage() -> None:
    assert is_outage_death({"user_input_reason": "Unable to connect to API"}) is True


def test_signature_in_error_arg_is_outage() -> None:
    assert is_outage_death({"summary": "ok"}, error="connection refused") is True


def test_api_error_with_connection_cooccurrence_is_outage() -> None:
    assert is_outage_death({"summary": "API Error: connection reset by peer"}) is True


def test_api_error_alone_is_not_outage() -> None:
    assert is_outage_death({"summary": "Added API Error handling and retries"}) is False


def test_legit_completion_is_not_outage() -> None:
    assert is_outage_death({"summary": "Implemented the recover command and tests"}) is False


def test_empty_result_is_not_outage() -> None:
    assert is_outage_death({}) is False
