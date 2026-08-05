"""The reopened-issue predicate — the one fact rule E may act on (#4152).

"The issue is open" is NOT that fact: a delivered ticket whose issue simply never
closed is open too, and reviving that one re-runs the whole ladder on work the
factory already shipped. These lanes pin the discriminator and, just as load-bearing,
every shape that must collapse to UNKNOWN rather than be guessed at.
"""

from teatree.backends.issue_reopen import reopen_state_from_payload
from teatree.core.backend_protocols import IssueReopenState


class TestReopenedIsDistinctFromMerelyOpen:
    def test_open_with_a_reopened_reason_is_reopened(self) -> None:
        payload = {"state": "open", "state_reason": "reopened"}

        assert reopen_state_from_payload(payload) is IssueReopenState.REOPENED

    def test_open_that_never_closed_is_not_reopened(self) -> None:
        """The false-positive population: delivered, issue never closed. Must not revive."""
        payload = {"state": "open", "state_reason": None}

        assert reopen_state_from_payload(payload) is IssueReopenState.NOT_REOPENED

    def test_a_closed_issue_is_not_reopened(self) -> None:
        assert reopen_state_from_payload({"state": "closed", "state_reason": "completed"}) is (
            IssueReopenState.NOT_REOPENED
        )

    def test_a_not_planned_close_is_not_reopened(self) -> None:
        assert reopen_state_from_payload({"state": "closed", "state_reason": "not_planned"}) is (
            IssueReopenState.NOT_REOPENED
        )

    def test_the_graphql_upper_case_reason_is_read_the_same(self) -> None:
        assert reopen_state_from_payload({"state": "OPEN", "state_reason": "REOPENED"}) is IssueReopenState.REOPENED


class TestUnclassifiableShapesAreUnknown:
    """UNKNOWN is fail-CLOSED: rule E revives on this verdict, so a guess is a re-run."""

    def test_a_payload_with_no_reopen_marker_at_all_is_unknown(self) -> None:
        """GitLab's issue object carries no ``state_reason`` — absence is never "not reopened"."""
        assert reopen_state_from_payload({"state": "opened", "closed_at": None}) is IssueReopenState.UNKNOWN

    def test_an_error_envelope_is_unknown(self) -> None:
        assert reopen_state_from_payload({"error": "Not a GitHub issue URL: x"}) is IssueReopenState.UNKNOWN

    def test_a_non_dict_is_unknown(self) -> None:
        assert reopen_state_from_payload(None) is IssueReopenState.UNKNOWN
        assert reopen_state_from_payload([{"state": "open"}]) is IssueReopenState.UNKNOWN

    def test_a_missing_or_non_string_state_is_unknown(self) -> None:
        assert reopen_state_from_payload({}) is IssueReopenState.UNKNOWN
        assert reopen_state_from_payload({"state": 7}) is IssueReopenState.UNKNOWN
