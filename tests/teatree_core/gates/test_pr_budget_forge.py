"""Forge-authoritative open-PR count for the per-(repo, ticket) budget (fleet-safety Stage 3).

Against a STUBBED code host: a sibling instance's open PR (footer-linked to the
ticket, same repo) is returned; a PR in another repo or for another ticket is
dropped (repo-scoping + ticket attribution); a forge transport error degrades to
``None`` (fail OPEN); and the per-``(ticket, repo)`` memo collapses the two ship
seams to one ``list_my_prs`` call.
"""

from django.test import TestCase

from teatree.core.gates.pr_budget_forge import (
    cached_forge_open_pr_urls_for_ticket,
    forge_open_pr_urls_for_ticket,
    reset_forge_pr_budget_cache,
)
from teatree.core.models import ConfigSetting, Ticket
from teatree.utils.run import CommandFailedError

_REPO = "souliane/teatree"
_OTHER_REPO = "souliane/other"


def _gh_pr(*, repo: str, number: int, body: str) -> dict:
    return {
        "html_url": f"https://github.com/{repo}/pull/{number}",
        "number": number,
        "body": body,
    }


def _forge_error() -> CommandFailedError:
    return CommandFailedError(["gh", "api", "search/issues"], 1, "", "API rate limit exceeded")


class _FakeHost:
    """Stub ``CodeHostBackend`` — only the surface the forge budget query touches."""

    def __init__(self, prs: list[dict], *, user: str = "fleet-bot", error: Exception | None = None) -> None:
        self._prs = prs
        self._user = user
        self._error = error
        self.list_calls = 0
        self.queried_authors: list[str] = []

    def current_user(self) -> str:
        return self._user

    def list_my_prs(self, *, author: str, updated_after: str | None = None) -> list[dict]:
        del updated_after
        self.queried_authors.append(author)
        self.list_calls += 1
        if self._error is not None:
            raise self._error
        return list(self._prs)


class _ForgeBudgetTestCase(TestCase):
    def setUp(self) -> None:
        reset_forge_pr_budget_cache()

    def _ticket(self, number: int = 123, *, overlay: str = "t3-teatree", repo: str = _REPO) -> Ticket:
        return Ticket.objects.create(
            overlay=overlay,
            issue_url=f"https://github.com/{repo}/issues/{number}",
            state=Ticket.State.IN_REVIEW,
        )


class TestForgeOpenPrUrlsForTicket(_ForgeBudgetTestCase):
    def test_returns_sibling_pr_url_the_local_db_does_not_know(self) -> None:
        ticket = self._ticket(123)
        host = _FakeHost([_gh_pr(repo=_REPO, number=501, body="feat: x\n\nCloses #123")])

        urls = forge_open_pr_urls_for_ticket(ticket, _REPO, host=host)

        assert urls == {"https://github.com/souliane/teatree/pull/501"}

    def test_drops_pr_in_a_different_repo(self) -> None:
        # Repo-scoping: a footer-linked PR living in another repo must not count.
        ticket = self._ticket(123)
        host = _FakeHost([_gh_pr(repo=_OTHER_REPO, number=7, body="Closes #123")])

        assert forge_open_pr_urls_for_ticket(ticket, _REPO, host=host) == set()

    def test_drops_pr_for_a_different_ticket(self) -> None:
        ticket = self._ticket(123)
        host = _FakeHost([_gh_pr(repo=_REPO, number=8, body="Closes #999")])

        assert forge_open_pr_urls_for_ticket(ticket, _REPO, host=host) == set()

    def test_drops_footerless_pr(self) -> None:
        ticket = self._ticket(123)
        host = _FakeHost([_gh_pr(repo=_REPO, number=9, body="no close footer here")])

        assert forge_open_pr_urls_for_ticket(ticket, _REPO, host=host) == set()

    def test_none_host_fails_open(self) -> None:
        assert forge_open_pr_urls_for_ticket(self._ticket(123), _REPO, host=None) is None

    def test_forge_error_fails_open(self) -> None:
        ticket = self._ticket(123)
        host = _FakeHost([], error=_forge_error())

        assert forge_open_pr_urls_for_ticket(ticket, _REPO, host=host) is None

    def test_empty_identity_fails_open(self) -> None:
        ticket = self._ticket(123)
        host = _FakeHost([_gh_pr(repo=_REPO, number=1, body="Closes #123")], user="")

        assert forge_open_pr_urls_for_ticket(ticket, _REPO, host=host) is None


class TestForgeIdentityAndDedup(_ForgeBudgetTestCase):
    def test_union_queries_configured_aliases_and_dedups_across_them(self) -> None:
        # Configured identity aliases win over current_user (mirrors MyPrsScanner)
        # and a PR returned under both aliases is counted once.
        ConfigSetting.objects.set_value(
            "user_identity_aliases",
            value=["alias-one", "alias-two"],
            scope="t3-teatree",
        )
        ticket = self._ticket(123)
        host = _FakeHost([_gh_pr(repo=_REPO, number=501, body="Closes #123")])

        urls = forge_open_pr_urls_for_ticket(ticket, _REPO, host=host)

        assert urls == {"https://github.com/souliane/teatree/pull/501"}
        assert host.queried_authors == ["alias-one", "alias-two"]

    def test_skips_a_pr_with_no_url(self) -> None:
        ticket = self._ticket(123)
        host = _FakeHost([{"number": 9, "body": "Closes #123"}])  # no html_url/web_url

        assert forge_open_pr_urls_for_ticket(ticket, _REPO, host=host) == set()

    def test_matches_a_gitlab_shaped_pr_by_description_and_web_url(self) -> None:
        ticket = self._ticket(50, repo="group/proj")
        gitlab_pr = {
            "web_url": "https://gitlab.com/group/proj/-/merge_requests/12",
            "iid": 12,
            "description": "feat: y\n\nCloses #50",
        }
        host = _FakeHost([gitlab_pr])

        urls = forge_open_pr_urls_for_ticket(ticket, "group/proj", host=host)

        assert urls == {"https://gitlab.com/group/proj/-/merge_requests/12"}


class TestCachedForgeOpenPrUrlsForTicket(_ForgeBudgetTestCase):
    def test_second_call_within_window_reuses_one_list_my_prs_call(self) -> None:
        ticket = self._ticket(123)
        host = _FakeHost([_gh_pr(repo=_REPO, number=501, body="Closes #123")])

        first = cached_forge_open_pr_urls_for_ticket(ticket, _REPO, host=host)
        second = cached_forge_open_pr_urls_for_ticket(ticket, _REPO, host=host)

        assert first == second == {"https://github.com/souliane/teatree/pull/501"}
        assert host.list_calls == 1

    def test_failure_is_not_cached_and_is_retried(self) -> None:
        ticket = self._ticket(123)
        host = _FakeHost([], error=_forge_error())

        assert cached_forge_open_pr_urls_for_ticket(ticket, _REPO, host=host) is None
        assert cached_forge_open_pr_urls_for_ticket(ticket, _REPO, host=host) is None
        assert host.list_calls == 2
