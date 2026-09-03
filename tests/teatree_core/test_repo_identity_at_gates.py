"""Repo-scoped gates compare REPOSITORY IDENTITY, never a path or a raw substring.

Two ship-path checks read the wrong thing:

*   the close-trailer publish gate matched its ``eng-group/*`` namespace patterns
    against the local CHECKOUT PATH, which never looks like a namespace — so a
    banned repo kept its ``Closes #N`` and merging the PR closed the issue against
    policy;
*   the created-PR verification accepted any URL merely CONTAINING the expected
    slug, so a live PR on ``<slug>-mirror`` verified as the right repo.
"""

from pathlib import Path
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.backend_protocols import PrOpenState, PullRequestSpec
from teatree.core.intake.close_trailer_scanner import apply_publish_gate
from teatree.core.models import Ticket, Worktree
from teatree.core.runners.ship import ShipExecutor

_BODY = "feat: subject\n\nCloses #1234"


class TestPublishGateReadsTheRepoSlug:
    def test_banned_slug_strips(self) -> None:
        assert "Closes" not in apply_publish_gate(_BODY, repo="eng-group/product", patterns=["eng-group/*"])

    def test_unbanned_slug_keeps(self) -> None:
        assert "Closes #1234" in apply_publish_gate(_BODY, repo="souliane/teatree", patterns=["eng-group/*"])

    def test_no_patterns_is_a_no_op_even_with_an_unidentifiable_repo(self) -> None:
        assert "Closes #1234" in apply_publish_gate(_BODY, repo="", patterns=[])

    def test_unidentifiable_repo_with_patterns_configured_fails_closed(self) -> None:
        assert "Closes" not in apply_publish_gate(_BODY, repo="", patterns=["eng-group/*"])


class _FakeHost:
    def current_user(self) -> str:
        return "tester"

    def is_assignable(self, *, repo: str, login: str) -> bool:
        return True

    def create_pr(self, spec: PullRequestSpec) -> dict[str, str]:
        return {"web_url": "https://example.com/pr/1"}


# ast-grep-ignore: ac-django-no-pytest-django-db
@pytest.mark.django_db
class TestShipResolvesTheSlugFromTheCheckout:
    def test_a_checkout_path_in_a_banned_namespace_strips_the_trailer(self, tmp_path: Path) -> None:
        ticket = Ticket.objects.create(
            overlay="", state=Ticket.State.REVIEWED, issue_url="https://example.com/issues/1"
        )
        Worktree.objects.create(
            overlay="",
            ticket=ticket,
            repo_path="product",
            branch="feat-x",
            extra={"worktree_path": str(tmp_path)},
        )
        with (
            patch(
                "teatree.core.runners.ship.git.last_commit_message",
                return_value=("feat: subject", "Closes #1234"),
            ),
            patch("teatree.core.runners.ship.git.config_value", return_value="tester"),
            patch("teatree.core.runners.ship.git.remote_slug", return_value="eng-group/product"),
            patch("teatree.core.runners.ship.get_overlay_publish_gates", return_value=["eng-group/*"]),
        ):
            spec = ShipExecutor._build_pr_spec(ticket, _FakeHost(), str(tmp_path / "worktrees" / "1234"), "feat-x", {})

        assert "Closes" not in spec.description

    def test_an_unbanned_slug_keeps_the_trailer(self, tmp_path: Path) -> None:
        ticket = Ticket.objects.create(
            overlay="", state=Ticket.State.REVIEWED, issue_url="https://example.com/issues/2"
        )
        Worktree.objects.create(
            overlay="",
            ticket=ticket,
            repo_path="teatree",
            branch="feat-y",
            extra={"worktree_path": str(tmp_path)},
        )
        with (
            patch(
                "teatree.core.runners.ship.git.last_commit_message",
                return_value=("feat: subject", "Closes #1234"),
            ),
            patch("teatree.core.runners.ship.git.config_value", return_value="tester"),
            patch("teatree.core.runners.ship.git.remote_slug", return_value="souliane/teatree"),
            patch("teatree.core.runners.ship.get_overlay_publish_gates", return_value=["eng-group/*"]),
        ):
            spec = ShipExecutor._build_pr_spec(ticket, _FakeHost(), str(tmp_path / "wt"), "feat-y", {})

        assert "Closes #1234" in spec.description


class _MirrorHost:
    """A backend that returns a LIVE PR URL for a different, longer-named repo.

    The re-read confirms it, so only the slug check can catch it.
    """

    def create_pr(self, spec: PullRequestSpec) -> dict[str, str]:
        return {"web_url": "https://example.com/expected-org/expected-repo-mirror/pull/9"}

    def get_pr_open_state(self, *, pr_url: str) -> PrOpenState:
        return PrOpenState.OPEN


# ast-grep-ignore: ac-django-no-pytest-django-db
@pytest.mark.django_db
class TestCreatedPrUrlMustNameTheExpectedRepoExactly(TestCase):
    def _ship(self, host: object) -> object:
        ticket = Ticket.objects.create(
            overlay="", state=Ticket.State.REVIEWED, issue_url="https://example.com/issues/3"
        )
        spec = PullRequestSpec(
            repo="/tmp/checkout",
            branch="feat-x",
            title="feat: x",
            description="feat: x",
            target_branch="main",
        )
        with (
            patch(
                "teatree.core.runners.ship.git.remote_slug",
                return_value="expected-org/expected-repo",
            ),
            patch("teatree.core.runners.ship.check_pr_budget", return_value=None),
            patch("teatree.core.runners.ship.evaluate_debt_delta", return_value=None),
        ):
            return ShipExecutor(ticket)._open_pr_and_record(ticket, host, spec)

    def test_a_sibling_mirror_repos_pr_url_is_refused(self) -> None:
        result = self._ship(_MirrorHost())
        assert result.ok is False
        assert "wrong repo" in result.detail
