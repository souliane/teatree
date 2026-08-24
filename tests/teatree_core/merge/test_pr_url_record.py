"""The shared writer both PR-opening paths record through (#4305).

The ship executor and the pre-push ``ensure-pr`` hook each open PRs; only the
first used to record the URL, so a refusal reached after the push left a live PR
the ticket had no record of. Both now write here.
"""

from django.test import TestCase

from teatree.core.merge.pr_url_record import record_pr_url
from teatree.core.models import PullRequest, Ticket

_URL = "https://github.com/souliane/teatree/pull/4305"
_BRANCH = "fix/4305-x"


class TestRecordPrUrl(TestCase):
    def _ticket(self, **extra: object) -> Ticket:
        return Ticket.objects.create(
            overlay="test",
            issue_url="https://github.com/souliane/teatree/issues/4305",
            extra=dict(extra),
        )

    def test_writes_the_list_the_branch_index_and_the_arbiter_row(self) -> None:
        ticket = self._ticket()

        record_pr_url(ticket, _URL, _BRANCH)

        ticket.refresh_from_db()
        assert ticket.extra["pr_urls"] == [_URL]
        assert ticket.extra["pr_url_by_branch"] == {_BRANCH: _URL}
        assert PullRequest.objects.get(url=_URL).ticket_id == ticket.pk

    def test_pop_keys_clears_the_callers_single_run_hints(self) -> None:
        ticket = self._ticket(pr_title_override="feat: pinned", ship_invoking_branch=_BRANCH)

        record_pr_url(ticket, _URL, _BRANCH, pop_keys=("pr_title_override", "ship_invoking_branch"))

        ticket.refresh_from_db()
        assert "pr_title_override" not in ticket.extra
        assert "ship_invoking_branch" not in ticket.extra

    def test_an_empty_url_records_nothing(self) -> None:
        # A caller with no URL has no PR to attribute; recording "" would put a
        # phantom entry in the index every later reader trusts.
        ticket = self._ticket()

        record_pr_url(ticket, "", _BRANCH)

        ticket.refresh_from_db()
        assert "pr_urls" not in ticket.extra
        assert "pr_url_by_branch" not in ticket.extra
        assert not PullRequest.objects.exists()

    def test_an_empty_branch_still_records_the_url_but_not_the_index(self) -> None:
        # The per-branch index is keyed by branch; an unnamed branch cannot key it.
        ticket = self._ticket()

        record_pr_url(ticket, _URL, "")

        ticket.refresh_from_db()
        assert ticket.extra["pr_urls"] == [_URL]
        assert "pr_url_by_branch" not in ticket.extra
