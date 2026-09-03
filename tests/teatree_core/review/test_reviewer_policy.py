"""The standing reviewer policy applied to MRs opened before the policy existed.

The scope checks are the whole point: reviewer-at-creation cannot reach an
already-open MR, but a general "assign this reviewer there" surface is exactly
what ``handle_block_self_reviewer_assign`` refuses. What keeps this narrow is
that neither side of the assignment can be named by a caller — the author must
be the identity the factory itself acts as, and the reviewers come from config.
"""

import inspect
from functools import partial
from unittest.mock import MagicMock

import pytest

from teatree.core.overlay import OverlayConfig
from teatree.core.review.reviewer_policy import ReviewerPolicyError, apply_reviewer_policy

BOT = "acme-factory-bot"
OWNER = "octocat"
REMOTE = "https://gitlab.com/acme-eng/factory.git"


def _overlay(*, reviewers: tuple[str, ...] = (OWNER,), bot_credentialed: bool = True) -> MagicMock:
    overlay = MagicMock()
    overlay.config.pr_auto_reviewers = list(reviewers)
    overlay.config.get_gitlab_token.return_value = "owner-token"
    overlay.config.get_gitlab_token_for_remote.return_value = "bot-token" if bot_credentialed else "owner-token"
    # The scope predicate is the REAL one, run against the stubbed getters above:
    # the creation-time half asks the same method, so a stub here would let the
    # two halves disagree about which repos the policy may touch.
    overlay.config.acts_as_distinct_identity_on.side_effect = partial(
        OverlayConfig.acts_as_distinct_identity_on,
        overlay.config,
    )
    return overlay


def _mr(iid: int, *, author: str, reviewers: tuple[str, ...] = ()) -> dict:
    return {
        "iid": iid,
        "web_url": f"https://gitlab.com/acme-eng/factory/-/merge_requests/{iid}",
        "author": {"username": author},
        "reviewers": [{"username": name} for name in reviewers],
    }


class FakeHost:
    def __init__(self, mrs: list[dict], *, user: str = BOT, unassignable: tuple[str, ...] = ()) -> None:
        self._mrs = mrs
        self._user = user
        self._unassignable = set(unassignable)
        self.assigned: list[tuple[str, str]] = []
        self.listed: tuple[str, str, str] | None = None

    def current_user(self) -> str:
        return self._user

    def list_prs(self, *, repo: str, state: str = "", author: str = "") -> list[dict]:
        self.listed = (repo, state, author)
        return self._mrs

    def assign_reviewer(self, *, pr_url: str, username: str) -> bool:
        if username in self._unassignable:
            return False
        self.assigned.append((pr_url, username))
        return True


class TestScopeIsEnforcedInCode:
    def test_a_human_authored_mr_is_refused(self) -> None:
        host = FakeHost([_mr(87, author="someone.else")])

        report = apply_reviewer_policy(_overlay(), host, remote=REMOTE)

        assert [row.outcome for row in report.rows] == ["refused"]
        assert host.assigned == []

    def test_the_owners_own_mr_is_refused_even_though_the_owner_is_the_reviewer(self) -> None:
        """The case the gate most cares about: never touch the human's own MR."""
        host = FakeHost([_mr(87, author=OWNER)])

        report = apply_reviewer_policy(_overlay(reviewers=(OWNER,)), host, remote=REMOTE)

        assert report.rows[0].outcome == "refused"
        assert host.assigned == []

    def test_a_repo_the_factory_acts_as_the_owner_on_refuses_the_whole_run(self) -> None:
        host = FakeHost([_mr(1, author=OWNER)], user=OWNER)

        with pytest.raises(ReviewerPolicyError, match="own credential"):
            apply_reviewer_policy(_overlay(bot_credentialed=False), host, remote=REMOTE)

        assert host.assigned == []

    def test_no_configured_reviewers_refuses_the_whole_run(self) -> None:
        host = FakeHost([_mr(76, author=BOT)])

        with pytest.raises(ReviewerPolicyError, match="pr_auto_reviewers"):
            apply_reviewer_policy(_overlay(reviewers=()), host, remote=REMOTE)

        assert host.assigned == []

    def test_no_caller_can_name_a_reviewer(self) -> None:
        """The absence of a username parameter is what keeps this from being a hole."""
        parameters = inspect.signature(apply_reviewer_policy).parameters

        assert not [name for name in parameters if "review" in name or "user" in name]


class TestApplication:
    def test_a_bot_authored_mr_missing_the_reviewer_is_assigned(self) -> None:
        host = FakeHost([_mr(76, author=BOT)])

        report = apply_reviewer_policy(_overlay(), host, remote=REMOTE)

        assert report.rows[0].outcome == "assigned"
        assert host.assigned == [("https://gitlab.com/acme-eng/factory/-/merge_requests/76", OWNER)]
        assert report.failed is False

    def test_an_mr_already_carrying_the_reviewer_is_a_noop(self) -> None:
        host = FakeHost([_mr(76, author=BOT, reviewers=(OWNER,))])

        report = apply_reviewer_policy(_overlay(), host, remote=REMOTE)

        assert report.rows[0].outcome == "unchanged"
        assert host.assigned == []
        assert report.failed is False

    def test_only_open_mrs_are_considered(self) -> None:
        host = FakeHost([])

        apply_reviewer_policy(_overlay(), host, remote=REMOTE)

        assert host.listed == ("acme-eng/factory", "opened", "")

    def test_a_failed_assignment_is_reported_and_fails_the_run(self) -> None:
        host = FakeHost([_mr(76, author=BOT)], unassignable=(OWNER,))

        report = apply_reviewer_policy(_overlay(), host, remote=REMOTE)

        assert report.rows[0].outcome == "failed"
        assert report.failed is True

    def test_dry_run_assigns_nothing(self) -> None:
        host = FakeHost([_mr(76, author=BOT)])

        report = apply_reviewer_policy(_overlay(), host, remote=REMOTE, dry_run=True)

        assert host.assigned == []
        assert "would assign" in report.rows[0].detail

    def test_every_mr_is_reported_including_the_refused_ones(self) -> None:
        host = FakeHost(
            [
                _mr(76, author=BOT),
                _mr(77, author=BOT, reviewers=(OWNER,)),
                _mr(87, author=OWNER),
            ],
        )

        report = apply_reviewer_policy(_overlay(), host, remote=REMOTE)

        assert [row.outcome for row in report.rows] == ["assigned", "unchanged", "refused"]
        assert len(report.lines()) == 3
