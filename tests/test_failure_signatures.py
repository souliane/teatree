"""The dependency-free phrase tables every failure reader consults."""

import pytest

from teatree.failure_signatures import (
    is_spawn_failure,
    is_transient_failure,
    outage_signature_in_text,
    quota_exhausted,
    quota_signature,
    transient_failure_signature,
)


class TestOutageSignatureInText:
    @pytest.mark.parametrize(
        "text",
        [
            "Unable to connect to API",
            "ConnectionRefused while dispatching",
            "FailedToOpenSocket",
            "safety classifier unavailable",
        ],
    )
    def test_connection_signature_is_outage(self, text: str) -> None:
        assert outage_signature_in_text(text)

    @pytest.mark.parametrize(
        "text",
        [
            "UNABLE TO CONNECT TO API",
            "connectionrefused",
            "FAILEDTOOPENSOCKET",
            "SAFETY CLASSIFIER UNAVAILABLE",
        ],
    )
    def test_matching_is_case_insensitive(self, text: str) -> None:
        assert outage_signature_in_text(text)

    def test_api_error_with_connection_cooccurrence_is_outage(self) -> None:
        assert outage_signature_in_text("API Error: connection reset by peer") == "api error + connect"

    def test_api_error_alone_is_not_outage(self) -> None:
        assert outage_signature_in_text("Added API Error handling and retries") == ""

    def test_legit_completion_is_not_outage(self) -> None:
        assert outage_signature_in_text("Implemented the recover command and tests") == ""

    def test_empty_text_is_not_outage(self) -> None:
        assert outage_signature_in_text("") == ""


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


class TestQuotaSignature:
    """Quota exhaustion is a BACKEND-capacity verdict, not a verdict about the code."""

    @pytest.mark.parametrize(
        "stderr",
        [
            "You have hit your usage limit for this plan.",
            "rate limit exceeded, retry after 3600s",
            "quota exhausted for the current billing period",
            "insufficient credit balance",
            "plan limit reached",
            "HTTP 429 Too Many Requests",
        ],
    )
    def test_exhaustion_stderr_is_classified(self, stderr: str) -> None:
        assert quota_signature(stderr)

    def test_matching_is_case_insensitive(self) -> None:
        assert quota_signature("USAGE LIMIT REACHED")

    @pytest.mark.parametrize(
        "stderr",
        [
            "error: no such file or directory",
            "panic: nil pointer dereference",
            "",
            "   ",
        ],
    )
    def test_ordinary_failures_are_not_quota(self, stderr: str) -> None:
        assert quota_signature(stderr) == ""


_REVIEW_BODY_DISCUSSING_LIMITS = (
    "## Findings\n"
    "1. `fetch_pages` has no rate limit backoff — a 429 from the forge retries "
    "immediately and burns the remaining quota. Suggest exponential backoff.\n"
    "Verdict: hold"
)


class TestQuotaExhaustedIsScopedToFailedRunStderr:
    """The false-positive control: a review that WORKED must never cool the backend.

    A review whose findings discuss rate limiting is a successful review. Feeding its
    body to the classifier would cool the backend down for hours over a job that did
    its work — so the trip decision reads ONLY stderr, and only on a non-zero exit.
    """

    def test_a_successful_run_never_trips_however_its_output_reads(self) -> None:
        assert quota_signature(_REVIEW_BODY_DISCUSSING_LIMITS)  # the body WOULD match
        assert quota_exhausted(returncode=0, stderr=_REVIEW_BODY_DISCUSSING_LIMITS) == ""

    def test_a_failed_run_with_an_ordinary_error_does_not_trip(self) -> None:
        assert quota_exhausted(returncode=1, stderr="error: no such file or directory") == ""

    def test_a_failed_run_whose_stderr_shows_exhaustion_trips(self) -> None:
        assert quota_exhausted(returncode=1, stderr="You have hit your usage limit") == "usage limit"


class TestIsSpawnFailure:
    """An agent that could not START and work that FAILED are different reports (#4301)."""

    def test_matches_the_named_e2big_refusal(self) -> None:
        assert is_spawn_failure(
            "agent could not be spawned: a single spawn argument is 181072 bytes, over this "
            "platform's 131072-byte per-argument limit (E2BIG)."
        )

    def test_matches_a_pre_fix_raw_traceback(self) -> None:
        # Attempts recorded before the named error existed carry only the SDK text.
        assert is_spawn_failure(
            "claude_agent_sdk._errors.CLIConnectionError: Failed to start Claude Code: "
            "[Errno 7] Argument list too long: '.../claude'"
        )

    def test_does_not_match_a_run_that_started_and_failed(self) -> None:
        assert not is_spawn_failure("AssertionError: expected 3 got 4")
        assert not is_spawn_failure("stuck_loop: lease lost for task 1: re-claimed in-process")
        assert not is_spawn_failure("")


class TestAHarnessCrashIsRequeued:
    """A crash of the agent PROCESS is environmental, so it must not stay terminal (#4439).

    ``FailureKind.HARNESS_CRASH`` sits in the taxonomy's ``_ENVIRONMENTAL`` set — "caused by
    the environment rather than by a defect in the work" — yet the requeue predicate did not
    recognise it, so the sweep never reopened one. Measured cost: eleven tasks dropped in a
    single day, and PR #4485 reached the owner unreviewed because its reviewing task died
    this way and nothing retried it.

    Requeue is safe here because it is bounded twice over: the #2009 repair-loop budget caps
    iterations, and two consecutive identical failures are escalated LOUDLY rather than
    reopened — so a deterministic crash halts and surfaces instead of looping.
    """

    def test_a_raw_process_traceback_is_transient(self) -> None:
        assert is_transient_failure("Traceback (most recent call last):\n  File ...\nException: boom")

    def test_the_sdk_control_request_timeout_is_transient(self) -> None:
        error = "Traceback (most recent call last):\nException: Control request timeout: initialize"
        assert is_transient_failure(error)

    def test_a_processerror_is_transient(self) -> None:
        assert is_transient_failure("ProcessError: the agent process exited with code 1")

    def test_a_deterministic_refusal_is_still_not_transient(self) -> None:
        """The widening must not swallow real defects — these stay terminal."""
        for deterministic in (
            "missing required evidence for phase 'reviewing'",
            "review verdict recording refused: head mismatch",
            "assert 1 == 2",
            "stuck_loop: runtime ceiling exceeded",
        ):
            assert not is_transient_failure(deterministic), deterministic
