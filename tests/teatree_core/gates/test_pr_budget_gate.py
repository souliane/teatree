"""Per-(repo, ticket) open-PR budget gate — north-star PR-2 proof-case mechanism.

The gate reads ``max_open_prs_per_repo_per_ticket`` as data (constraint-as-data)
and refuses opening a PR when a ticket already has that many open (not-merged)
PRs in one repo. Anti-vacuity coverage. TestInertAtDefault: the neutral default
``0`` never refuses even with open PRs present (RED if the ``limit <= 0`` guard
goes). TestPerTicketPerRepoScope: at limit 1 a second open PR for the same
``(repo, ticket)`` is refused while the first, a different ticket's PR in the
same repo, and the same ticket's PR in a different repo are all allowed.
TestResolvePerOverlay: the limit flows through the real ``get_effective_settings``
per overlay (overlay A limited, overlay B unlimited).
"""

import pytest
from django.test import TestCase

from teatree.core.gates.pr_budget_gate import (
    PrBudgetExceededError,
    check_pr_budget,
    count_open_prs_for_repo,
    open_pr_urls_for_repo,
    resolve_pr_budget,
)
from teatree.core.models import ConfigSetting, PullRequest, Ticket

_REPO_A = "souliane/teatree"
_REPO_B = "souliane/other"


def _pr(ticket: Ticket, *, repo: str, iid: str, state: str = PullRequest.State.OPEN) -> PullRequest:
    return PullRequest.objects.create(
        ticket=ticket,
        url=f"https://github.com/{repo}/pull/{iid}",
        repo=repo,
        iid=iid,
        overlay=ticket.overlay,
        state=state,
    )


class TestCountOpenPrsForRepo(TestCase):
    def test_counts_open_rows_scoped_to_ticket_and_repo(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        _pr(ticket, repo=_REPO_A, iid="1")
        _pr(ticket, repo=_REPO_A, iid="2")
        assert count_open_prs_for_repo(ticket, _REPO_A) == 2

    def test_excludes_merged_rows(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        _pr(ticket, repo=_REPO_A, iid="1", state=PullRequest.State.MERGED)
        assert count_open_prs_for_repo(ticket, _REPO_A) == 0

    def test_excludes_other_repo_and_other_ticket(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        other = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        _pr(ticket, repo=_REPO_B, iid="1")
        _pr(other, repo=_REPO_A, iid="2")
        assert count_open_prs_for_repo(ticket, _REPO_A) == 0

    def test_unions_pr_url_by_branch_for_matching_repo(self) -> None:
        # A PR the reconciler has not yet upserted into a PullRequest row is
        # still in ``extra["pr_url_by_branch"]`` (written synchronously by the
        # ship executor) — so the union counts it.
        ticket = Ticket.objects.create(
            overlay="t3-teatree",
            state=Ticket.State.IN_REVIEW,
            extra={"pr_url_by_branch": {"feat/x": f"https://github.com/{_REPO_A}/pull/9"}},
        )
        assert count_open_prs_for_repo(ticket, _REPO_A) == 1

    def test_pr_url_by_branch_for_other_repo_does_not_count(self) -> None:
        ticket = Ticket.objects.create(
            overlay="t3-teatree",
            state=Ticket.State.IN_REVIEW,
            extra={"pr_url_by_branch": {"feat/x": f"https://github.com/{_REPO_B}/pull/9"}},
        )
        assert count_open_prs_for_repo(ticket, _REPO_A) == 0

    def test_union_dedups_a_url_present_in_both_sources(self) -> None:
        url = f"https://github.com/{_REPO_A}/pull/1"
        ticket = Ticket.objects.create(
            overlay="t3-teatree",
            state=Ticket.State.IN_REVIEW,
            extra={"pr_url_by_branch": {"feat/x": url}},
        )
        _pr(ticket, repo=_REPO_A, iid="1")  # same url as the extra entry
        assert count_open_prs_for_repo(ticket, _REPO_A) == 1


class TestInertAtDefault(TestCase):
    def test_zero_limit_never_refuses_even_with_open_prs(self) -> None:
        # Neutral default 0 = unlimited: core ships inert. Two open PRs for the
        # same (repo, ticket) do NOT trip the gate. Anti-vacuous: without the
        # ``limit <= 0`` short-circuit, ``count (2) >= 0`` would raise.
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        _pr(ticket, repo=_REPO_A, iid="1")
        _pr(ticket, repo=_REPO_A, iid="2")
        check_pr_budget(ticket, _REPO_A, limit=0)  # no raise

    def test_negative_limit_is_also_inert(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        _pr(ticket, repo=_REPO_A, iid="1")
        check_pr_budget(ticket, _REPO_A, limit=-1)  # no raise


class TestPerTicketPerRepoScope(TestCase):
    def test_second_open_pr_for_same_repo_ticket_is_refused_at_limit_one(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        _pr(ticket, repo=_REPO_A, iid="1")
        with pytest.raises(PrBudgetExceededError):
            check_pr_budget(ticket, _REPO_A, limit=1)

    def test_first_open_pr_is_allowed_at_limit_one(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        check_pr_budget(ticket, _REPO_A, limit=1)  # count 0 < 1 -> allowed

    def test_different_ticket_same_repo_is_allowed(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        other = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        _pr(ticket, repo=_REPO_A, iid="1")
        check_pr_budget(other, _REPO_A, limit=1)  # other ticket has 0 -> allowed

    def test_same_ticket_different_repo_is_allowed(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        _pr(ticket, repo=_REPO_A, iid="1")
        check_pr_budget(ticket, _REPO_B, limit=1)  # repo B has 0 -> allowed

    def test_merged_pr_does_not_consume_the_budget(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        _pr(ticket, repo=_REPO_A, iid="1", state=PullRequest.State.MERGED)
        check_pr_budget(ticket, _REPO_A, limit=1)  # merged excluded -> allowed

    def test_refusal_message_names_the_offending_url_repo_and_escape(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        pr = _pr(ticket, repo=_REPO_A, iid="1")
        with pytest.raises(PrBudgetExceededError) as excinfo:
            check_pr_budget(ticket, _REPO_A, limit=1)
        message = str(excinfo.value)
        assert pr.url in message
        assert _REPO_A in message
        assert "max_open_prs_per_repo_per_ticket" in message


class TestOpenPrUrlsForRepo(TestCase):
    def test_returns_the_deduped_union_url_set(self) -> None:
        url = f"https://github.com/{_REPO_A}/pull/1"
        ticket = Ticket.objects.create(
            overlay="t3-teatree",
            state=Ticket.State.IN_REVIEW,
            extra={"pr_url_by_branch": {"feat/x": url, "feat/y": f"https://github.com/{_REPO_A}/pull/2"}},
        )
        _pr(ticket, repo=_REPO_A, iid="1")  # same url as feat/x
        assert open_pr_urls_for_repo(ticket, _REPO_A) == {url, f"https://github.com/{_REPO_A}/pull/2"}


class TestResolvePerOverlay(TestCase):
    @pytest.fixture(autouse=True)
    def _config_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)

    def test_limit_flows_through_get_effective_settings_per_overlay(self) -> None:
        # Overlay A opts into a cap of 1; overlay B never set it -> stays 0
        # (unlimited). The per-overlay ConfigSetting row is the sole source.
        ConfigSetting.objects.set_value("max_open_prs_per_repo_per_ticket", value=1, scope="overlay-a")
        assert resolve_pr_budget("overlay-a") == 1
        assert resolve_pr_budget("overlay-b") == 0

    def test_default_is_zero_when_unset(self) -> None:
        assert resolve_pr_budget(None) == 0
