"""The dependency-free phrase tables every failure reader consults.

These predicates answer only "what does this text SAY?". What to DO about a failure is
keyed on its named kind — ``tests/teatree_core/modelkit/test_task_failure_taxonomy.py``.
"""

import pytest

from teatree.failure_signatures import is_spawn_failure, outage_signature_in_text, quota_exhausted, quota_signature


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
