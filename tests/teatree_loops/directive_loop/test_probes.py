"""The behavior-probe catalog — VERIFYING evidence class 3 (north-star PR-7).

``pr_budget_violations`` is the proof-case probe: it finds a ``(ticket, repo)`` in
scope breaching the activated open-PR budget, and is clean when the constraint holds.
"""

from datetime import UTC, datetime
from unittest.mock import patch

from django.test import TestCase

from teatree.core.models import ConfigSetting, PullRequest, Ticket
from teatree.loops.directive_loop.probes import pr_budget_violations, resolve_probe

_SCOPE = "t3-teatree"
_SINCE = datetime(2026, 1, 1, tzinfo=UTC)


def _pr(ticket: Ticket, iid: str) -> None:
    PullRequest.objects.create(
        ticket=ticket, overlay=_SCOPE, url=f"https://github.com/o/r/pull/{iid}", repo="o/r", iid=iid
    )


class TestPrBudgetViolations(TestCase):
    def test_no_explicit_limit_uses_the_shipped_default_of_one(self) -> None:
        # D9: the shipped default is 1, so two open PRs with no explicit override breach it.
        ticket = Ticket.objects.create(issue_url="https://e.com/1", role=Ticket.Role.AUTHOR, overlay=_SCOPE)
        _pr(ticket, "1")
        _pr(ticket, "2")
        finding = pr_budget_violations(_SCOPE, _SINCE)
        assert finding is not None
        assert "limit 1" in finding

    def test_explicit_zero_is_the_unlimited_opt_out(self) -> None:
        ConfigSetting.objects.set_value("max_open_prs_per_repo_per_ticket", 0, scope=_SCOPE)
        ticket = Ticket.objects.create(issue_url="https://e.com/1b", role=Ticket.Role.AUTHOR, overlay=_SCOPE)
        _pr(ticket, "1")
        _pr(ticket, "2")
        assert pr_budget_violations(_SCOPE, _SINCE) is None

    def test_within_the_limit_is_clean(self) -> None:
        ConfigSetting.objects.set_value("max_open_prs_per_repo_per_ticket", 1, scope=_SCOPE)
        ticket = Ticket.objects.create(issue_url="https://e.com/2", role=Ticket.Role.AUTHOR, overlay=_SCOPE)
        _pr(ticket, "3")
        assert pr_budget_violations(_SCOPE, _SINCE) is None

    def test_a_breach_is_flagged(self) -> None:
        ConfigSetting.objects.set_value("max_open_prs_per_repo_per_ticket", 1, scope=_SCOPE)
        ticket = Ticket.objects.create(issue_url="https://e.com/3", role=Ticket.Role.AUTHOR, overlay=_SCOPE)
        _pr(ticket, "4")
        _pr(ticket, "5")
        finding = pr_budget_violations(_SCOPE, _SINCE)
        assert finding is not None
        assert "2 open PRs" in finding

    def test_a_pr_whose_ticket_vanished_is_skipped(self) -> None:
        # A PR row whose ticket was deleted (FK cascade normally prevents this, but a
        # values_list read can outrun a delete) is skipped, not a crash.
        ConfigSetting.objects.set_value("max_open_prs_per_repo_per_ticket", 1, scope=_SCOPE)
        ticket = Ticket.objects.create(issue_url="https://e.com/gone", role=Ticket.Role.AUTHOR, overlay=_SCOPE)
        _pr(ticket, "8")
        _pr(ticket, "9")
        with patch.object(Ticket.objects, "filter", return_value=Ticket.objects.none()):
            assert pr_budget_violations(_SCOPE, _SINCE) is None

    def test_a_merged_pr_does_not_count(self) -> None:
        ConfigSetting.objects.set_value("max_open_prs_per_repo_per_ticket", 1, scope=_SCOPE)
        ticket = Ticket.objects.create(issue_url="https://e.com/4", role=Ticket.Role.AUTHOR, overlay=_SCOPE)
        _pr(ticket, "6")
        merged = PullRequest.objects.create(
            ticket=ticket, overlay=_SCOPE, url="https://github.com/o/r/pull/7", repo="o/r", iid="7"
        )
        merged.mark_merged()
        merged.save()
        assert pr_budget_violations(_SCOPE, _SINCE) is None


class TestResolveProbe:
    def test_resolves_a_catalog_entry(self) -> None:
        assert resolve_probe("pr_budget_violations") is pr_budget_violations

    def test_unknown_or_empty_is_none(self) -> None:
        assert resolve_probe("") is None
        assert resolve_probe("nonexistent") is None
