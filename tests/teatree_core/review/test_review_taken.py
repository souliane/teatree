"""The forge-side stop: a colleague's note or any approval means the review is taken (#159)."""

from collections.abc import Sequence

import pytest

from teatree.core.review.review_taken import ReviewTaken, is_bot_author, review_taken_by_other
from teatree.types import RawAPIDict

MR = "https://gitlab.com/acme/app/-/merge_requests/7"
SELF = ("alice", "factory-bot")


class _Host:
    def __init__(
        self,
        *,
        notes: Sequence[RawAPIDict] = (),
        approved_by: Sequence[str] = (),
        raises: Exception | None = None,
    ) -> None:
        self._notes = list(notes)
        self._approved_by = list(approved_by)
        self._raises = raises

    def list_pr_comments(self, *, repo: str, pr_iid: int) -> list[RawAPIDict]:
        _ = (repo, pr_iid)
        if self._raises is not None:
            raise self._raises
        return list(self._notes)

    def get_mr_approvals(self, *, repo: str, pr_iid: int) -> dict[str, object]:
        _ = (repo, pr_iid)
        return {"approvals_left": 0, "approved_by": list(self._approved_by), "unresolved_resolvable": 0}


def _note(author: str, *, system: bool = False) -> RawAPIDict:
    return {"system": system, "author": {"username": author}, "body": "looks off"}


class TestReviewTakenByOther:
    def test_colleague_note_takes_the_review(self) -> None:
        host = _Host(notes=[_note("bob")])
        assert review_taken_by_other(MR, self_identities=SELF, host=host) is ReviewTaken.TAKEN

    def test_own_notes_do_not(self) -> None:
        host = _Host(notes=[_note("alice"), _note("factory-bot")])
        assert review_taken_by_other(MR, self_identities=SELF, host=host) is ReviewTaken.FREE

    def test_system_note_does_not(self) -> None:
        host = _Host(notes=[_note("bob", system=True)])
        assert review_taken_by_other(MR, self_identities=SELF, host=host) is ReviewTaken.FREE

    def test_access_token_bot_note_does_not(self) -> None:
        host = _Host(notes=[_note("project_42_bot_9f8e")])
        assert review_taken_by_other(MR, self_identities=SELF, host=host) is ReviewTaken.FREE

    def test_github_bot_comment_does_not(self) -> None:
        host = _Host(notes=[{"user": {"login": "dependabot[bot]", "type": "Bot"}, "body": "bump"}])
        assert review_taken_by_other(MR, self_identities=SELF, host=host) is ReviewTaken.FREE

    def test_github_human_comment_takes_the_review(self) -> None:
        host = _Host(notes=[{"user": {"login": "carol", "type": "User"}, "body": "nit"}])
        assert review_taken_by_other(MR, self_identities=SELF, host=host) is ReviewTaken.TAKEN

    def test_any_approval_takes_the_review_even_the_owners(self) -> None:
        host = _Host(approved_by=["alice"])
        assert review_taken_by_other(MR, self_identities=SELF, host=host) is ReviewTaken.TAKEN

    def test_read_error_is_unknown_not_free(self) -> None:
        host = _Host(raises=RuntimeError("forge down"))
        assert review_taken_by_other(MR, self_identities=SELF, host=host) is ReviewTaken.UNKNOWN

    def test_unparseable_url_is_unknown(self) -> None:
        assert review_taken_by_other("https://example.com/x", self_identities=SELF, host=_Host()) is ReviewTaken.UNKNOWN

    def test_no_host_is_unknown(self) -> None:
        assert review_taken_by_other(MR, self_identities=SELF, host=None) is ReviewTaken.UNKNOWN


class TestIsBotAuthor:
    @pytest.mark.parametrize(
        ("username", "user_type", "expected"),
        [
            ("project_42_bot_9f8e", "", True),
            ("group_7_bot_abc", "", True),
            ("dependabot[bot]", "", True),
            ("anything", "Bot", True),
            ("robot", "", False),
            ("chatbot_x", "", False),
            ("bob", "User", False),
        ],
    )
    def test_shapes(self, *, username: str, user_type: str, expected: bool) -> None:
        assert is_bot_author(username, user_type=user_type) is expected
