from django.test import SimpleTestCase

from teatree.core.worktree.stuck_pr_notice import plain_failure_cause


class StuckPrNoticeTest(SimpleTestCase):
    def test_cause_is_plain_and_does_not_replay_a_raw_error(self) -> None:
        assert plain_failure_cause("branch not on remote yet") == "The branch has not reached its remote yet."
        assert plain_failure_cause("unknown: /secret/path") == "The last attempt could not open the pull request."
