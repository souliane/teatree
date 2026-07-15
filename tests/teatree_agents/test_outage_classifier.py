"""Pure outage-death classifier consulted by the recorder chokepoint (#1764)."""

import pytest

from teatree.agents.outage_classifier import is_outage_death, is_transient_failure, transient_failure_signature


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


class TestTransientFailureClassifier:
    """The FAILED-attempt error-string classifier the bounded auto-requeue sweep consults.

    A transient failure is an infrastructure interruption (outage envelope,
    provisioning-step failure, an incomplete run that left no terminal
    ResultMessage, a coder yield that landed no commit). A deterministic failure
    (a test failure, an assertion, a real bug, a schema/evidence refusal) is NOT
    transient and must stay terminal FAILED.
    """

    @pytest.mark.parametrize(
        "error",
        [
            "outage_death: connection refused",
            "provision_failed: db import returned 0 rows",
            "result_error: no terminal ResultMessage — the run ended without completing",
            "result_error: subtype=error_during_execution — api_error_status=529",
            "landing_unverified: no new commit on the branch",
            "Unable to connect to API",
            "API Error: connection reset by peer",
        ],
    )
    def test_transient_errors_are_classified_transient(self, error: str) -> None:
        assert is_transient_failure(error) is True

    @pytest.mark.parametrize(
        "error",
        [
            "missing required evidence for phase 'coding': result must include one of [files_modified]",
            "Agent result contains unexpected keys: bogus",
            "review verdict recording refused: reviewer identity is a maker role",
            "AssertionError: expected 3 got 4",
            "test_widget_renders FAILED: ValueError",
            "stuck_loop: turns ceiling exceeded",
            "Added API Error handling and retries",
            "",
        ],
    )
    def test_deterministic_errors_are_not_transient(self, error: str) -> None:
        assert is_transient_failure(error) is False

    def test_signature_names_the_matched_class(self) -> None:
        assert transient_failure_signature("outage_death: x").startswith("outage_death")
        assert transient_failure_signature("landing_unverified: y").startswith("landing_unverified")
        assert transient_failure_signature("AssertionError: nope") == ""

    def test_classification_is_case_insensitive(self) -> None:
        assert is_transient_failure("RESULT_ERROR: NO TERMINAL RESULTMESSAGE") is True
