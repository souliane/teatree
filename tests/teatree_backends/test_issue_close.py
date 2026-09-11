"""The pure close classifier — CLOSED / OPEN / UNKNOWN from a raw forge payload (#4711).

The board retires a pre-ship ticket on a CLOSED verdict, so every shape this cannot
positively classify has to collapse to UNKNOWN: retiring live work because a forge read
came back malformed is the failure the third value exists to prevent.
"""

from teatree.backends.issue_close import close_verdict_from_payload
from teatree.core.backend_protocols import IssueOpenState


class TestClosedPayloads:
    def test_not_planned_carries_its_reason(self) -> None:
        verdict = close_verdict_from_payload({"state": "closed", "state_reason": "not_planned"})
        assert (verdict.state, verdict.reason) == (IssueOpenState.CLOSED, "not_planned")

    def test_completed_carries_its_reason(self) -> None:
        verdict = close_verdict_from_payload({"state": "closed", "state_reason": "completed"})
        assert (verdict.state, verdict.reason) == (IssueOpenState.CLOSED, "completed")

    def test_a_forge_with_no_reason_marker_still_reads_closed(self) -> None:
        """GitLab marks no ``state_reason``; the verdict must not weaken to UNKNOWN."""
        verdict = close_verdict_from_payload({"state": "closed"})
        assert (verdict.state, verdict.reason) == (IssueOpenState.CLOSED, "")

    def test_a_non_string_reason_is_dropped_not_stringified(self) -> None:
        verdict = close_verdict_from_payload({"state": "closed", "state_reason": None})
        assert (verdict.state, verdict.reason) == (IssueOpenState.CLOSED, "")

    def test_the_completed_state_spelling_reads_closed(self) -> None:
        assert close_verdict_from_payload({"state": "completed"}).state is IssueOpenState.CLOSED


class TestOpenPayloads:
    def test_github_open(self) -> None:
        assert close_verdict_from_payload({"state": "open"}).state is IssueOpenState.OPEN

    def test_gitlab_opened(self) -> None:
        assert close_verdict_from_payload({"state": "opened"}).state is IssueOpenState.OPEN

    def test_a_reopened_issue_is_open_not_closed(self) -> None:
        payload = {"state": "open", "state_reason": "reopened"}
        assert close_verdict_from_payload(payload).state is IssueOpenState.OPEN


class TestUnclassifiablePayloads:
    def test_an_error_envelope(self) -> None:
        assert close_verdict_from_payload({"error": "not a GitHub issue URL"}).state is IssueOpenState.UNKNOWN

    def test_a_non_dict(self) -> None:
        assert close_verdict_from_payload(["not", "a", "payload"]).state is IssueOpenState.UNKNOWN

    def test_a_missing_state(self) -> None:
        assert close_verdict_from_payload({"title": "no state here"}).state is IssueOpenState.UNKNOWN

    def test_a_non_string_state(self) -> None:
        assert close_verdict_from_payload({"state": 7}).state is IssueOpenState.UNKNOWN

    def test_a_state_no_forge_teatree_speaks_to_uses(self) -> None:
        assert close_verdict_from_payload({"state": "locked"}).state is IssueOpenState.UNKNOWN
