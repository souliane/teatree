"""``ticket_owning_pr_branch`` — the one scoped, unambiguous branch -> ticket answer."""

from pathlib import Path

import pytest
from django.test import TestCase

from teatree.core.models import Ticket, Worktree
from teatree.core.worktree.branch_owner import ticket_owning_pr_branch
from teatree.utils.run import run_checked

_SLUG = "souliane/teatree"
_BRANCH = "5145-merge-gate-b"
_ISSUE = "https://github.com/souliane/teatree/issues/5145"


def _ticket(
    issue_url: str = _ISSUE, *, state: str = Ticket.State.WORK_STARTED, role: str = Ticket.Role.AUTHOR
) -> Ticket:
    return Ticket.objects.create(overlay="t3-teatree", issue_url=issue_url, state=state, role=role)


def _row(ticket: Ticket, *, repo_path: str = _SLUG, branch: str = _BRANCH, worktree_path: str = "") -> Worktree:
    return Worktree.objects.create(
        ticket=ticket,
        overlay="t3-teatree",
        repo_path=repo_path,
        branch=branch,
        extra={"worktree_path": worktree_path} if worktree_path else {},
    )


class TestOwnsTheBranch(TestCase):
    def test_a_live_author_ticket_with_a_row_on_the_branch_owns_it(self) -> None:
        ticket = _ticket()
        _row(ticket)

        assert ticket_owning_pr_branch(_BRANCH, slug=_SLUG) == ticket

    def test_the_repo_slug_compares_case_insensitively(self) -> None:
        ticket = _ticket()
        _row(ticket, repo_path="Souliane/Teatree")

        assert ticket_owning_pr_branch(_BRANCH, slug=_SLUG) == ticket

    def test_a_ticket_keyed_by_the_pr_url_owns_its_branch(self) -> None:
        ticket = _ticket("https://github.com/souliane/teatree/pull/5159")
        _row(ticket)

        assert ticket_owning_pr_branch(_BRANCH, slug=_SLUG) == ticket

    def test_one_ticket_spanning_two_repos_resolves_in_each(self) -> None:
        ticket = _ticket()
        _row(ticket)
        _row(ticket, repo_path="souliane/private-skills")

        assert ticket_owning_pr_branch(_BRANCH, slug=_SLUG) == ticket
        assert ticket_owning_pr_branch(_BRANCH, slug="souliane/private-skills") == ticket

    def test_one_ticket_with_two_checkouts_of_the_branch_still_owns_it(self) -> None:
        ticket = _ticket()
        _row(ticket)
        _row(ticket, repo_path="Souliane/Teatree")

        assert ticket_owning_pr_branch(_BRANCH, slug=_SLUG) == ticket

    def test_a_per_branch_local_anchor_owns_exactly_its_own_branch(self) -> None:
        ticket = _ticket(f"auto:{_BRANCH}")
        _row(ticket)

        assert ticket_owning_pr_branch(_BRANCH, slug=_SLUG) == ticket


class TestOwnsNothing(TestCase):
    def test_no_row_on_the_branch(self) -> None:
        _row(_ticket(), branch="another-branch")

        assert ticket_owning_pr_branch(_BRANCH, slug=_SLUG) is None

    def test_another_repos_row_does_not_own_the_branch(self) -> None:
        _row(_ticket(), repo_path="souliane/private-skills")

        assert ticket_owning_pr_branch(_BRANCH, slug=_SLUG) is None

    def test_the_catch_all_local_anchor_never_owns_a_branch(self) -> None:
        _row(_ticket("auto:HEAD"))

        assert ticket_owning_pr_branch(_BRANCH, slug=_SLUG) is None

    def test_the_cadence_anchor_never_owns_a_branch(self) -> None:
        _row(_ticket("architectural-review://t3-teatree"))

        assert ticket_owning_pr_branch(_BRANCH, slug=_SLUG) is None

    def test_a_reviewer_role_ticket_never_owns_a_branch(self) -> None:
        _row(_ticket(role=Ticket.Role.REVIEWER))

        assert ticket_owning_pr_branch(_BRANCH, slug=_SLUG) is None

    def test_a_settled_ticket_never_owns_a_branch(self) -> None:
        for number, state in enumerate(sorted(Ticket.marker_release_states()), start=900):
            with self.subTest(state=state):
                Worktree.objects.all().delete()
                _row(_ticket(f"https://github.com/souliane/teatree/issues/{number}", state=state))

                assert ticket_owning_pr_branch(_BRANCH, slug=_SLUG) is None

    def test_two_distinct_live_tickets_are_ambiguous(self) -> None:
        _row(_ticket())
        _row(_ticket("https://github.com/souliane/teatree/issues/1"))

        assert ticket_owning_pr_branch(_BRANCH, slug=_SLUG) is None

    def test_an_empty_slug_matches_nothing(self) -> None:
        _row(_ticket(), repo_path="teatree")

        assert ticket_owning_pr_branch(_BRANCH, slug="") is None

    def test_an_empty_branch_matches_nothing(self) -> None:
        _row(_ticket(), branch="")

        assert ticket_owning_pr_branch("", slug=_SLUG) is None


class TestCloneLeafRepoPath(TestCase):
    @pytest.fixture(autouse=True)
    def _inject(self, tmp_path: Path) -> None:
        self._tmp = tmp_path

    def _checkout(self, origin: str | None) -> Path:
        checkout = self._tmp / "checkout"
        run_checked(["git", "init", "-b", "main", str(checkout)])
        if origin is not None:
            run_checked(["git", "remote", "add", "origin", origin], cwd=checkout)
        return checkout

    def test_a_clone_leaf_repo_path_resolves_through_the_checkouts_origin(self) -> None:
        ticket = _ticket()
        checkout = self._checkout("git@github.com:souliane/teatree.git")
        _row(ticket, repo_path="teatree-deploy", worktree_path=str(checkout))

        assert ticket_owning_pr_branch(_BRANCH, slug=_SLUG) == ticket

    def test_a_clone_leaf_whose_origin_names_another_repo_does_not_match(self) -> None:
        checkout = self._checkout("https://github.com/souliane/private-skills.git")
        _row(_ticket(), repo_path="teatree", worktree_path=str(checkout))

        assert ticket_owning_pr_branch(_BRANCH, slug=_SLUG) is None

    def test_an_unreadable_checkout_is_no_match(self) -> None:
        _row(_ticket(), repo_path="teatree-deploy", worktree_path=str(self._tmp / "gone"))
        _row(_ticket("https://github.com/souliane/teatree/issues/2"), repo_path="teatree-deploy")

        assert ticket_owning_pr_branch(_BRANCH, slug=_SLUG) is None

    def test_a_checkout_without_an_origin_is_no_match(self) -> None:
        _row(_ticket(), repo_path="teatree-deploy", worktree_path=str(self._checkout(None)))

        assert ticket_owning_pr_branch(_BRANCH, slug=_SLUG) is None
