"""A hand-off that counts a backlog it never locates hands over an unusable number."""

from teatree.core.handover import dangling_backlog_claims


class TestABacklogCountWithoutADurableHome:
    def test_a_bare_count_is_flagged(self) -> None:
        assert dangling_backlog_claims("71 tasks, 34 pending, the rest closed.") == [
            "71 tasks",
            "34 pending",
        ]

    def test_the_real_regression_is_flagged(self) -> None:
        """The wording that nearly lost a 56-item backlog: a count over a per-session store."""
        payload = (
            "## Task list\n\n"
            "71 tasks. Roughly 30 completed, 7 in progress, 34 pending — of which 12 are external\n"
            "asks and 4 are in a repo the owner has told the agent not to touch."
        )
        assert dangling_backlog_claims(payload)

    def test_a_count_beside_a_file_path_is_not_flagged(self) -> None:
        payload = "34 pending, listed in /Users/x/Desktop/backlog.md line 551."
        assert dangling_backlog_claims(payload) == []

    def test_a_count_beside_a_home_path_is_not_flagged(self) -> None:
        assert dangling_backlog_claims("34 pending — see ~/Desktop/backlog.md") == []

    def test_a_count_beside_a_url_is_not_flagged(self) -> None:
        assert dangling_backlog_claims("12 outstanding: https://example.invalid/board") == []

    def test_the_reference_must_share_the_paragraph(self) -> None:
        """A path elsewhere in the document does not locate this paragraph's count."""
        payload = "See /Users/x/notes.md for background.\n\n34 pending after today."
        assert dangling_backlog_claims(payload) == ["34 pending"]


class TestCountsThatAreNotBacklogs:
    def test_a_test_count_is_not_a_backlog(self) -> None:
        assert dangling_backlog_claims("551 tests passed, 0 failed.") == []

    def test_a_job_count_is_not_a_backlog(self) -> None:
        assert dangling_backlog_claims("19 jobs succeeded; 3925 tests in shard 1.") == []

    def test_an_empty_payload_has_no_claims(self) -> None:
        assert dangling_backlog_claims("") == []
