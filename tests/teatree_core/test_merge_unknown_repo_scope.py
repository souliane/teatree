"""The keystone merge refuses an UNKNOWN-repo target without human go-ahead.

SCOPE gate on the merge chokepoint: a CLEAR whose slug no registered overlay
owns is held for the operator (escalated, FSM untouched) the same way a
substrate CLEAR is — unless the operator re-presents a ``--human-authorized``
id. An OWNED slug (every ``souliane/*`` repo, via the always-registered
t3-teatree overlay) merges normally. The gate is opt-in (some overlay declared
``owned_repos``) and fail-open on a classification error.
"""

from typing import cast
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.models import MergeClear, Ticket

pytestmark = pytest.mark.django_db

_SHA = "a" * 40


def _ticket(slug: str, iid: int) -> Ticket:
    return Ticket.objects.create(issue_url=f"https://github.com/{slug}/issues/{iid}", short_description="t")


def _clear(ticket: Ticket, slug: str) -> MergeClear:
    return MergeClear.objects.create(
        ticket=ticket,
        pr_id=42,
        slug=slug,
        reviewed_sha=_SHA,
        reviewer_identity="cold-reviewer",
        gh_verify_result=MergeClear.VerifyResult.GREEN,
        blast_class=MergeClear.BlastClass.DOCS,
    )


def _merge(clear_id: int, **kwargs: str) -> dict:
    return cast("dict", call_command("ticket", "merge", str(clear_id), **kwargs))


class TestMergeUnknownRepoScope(TestCase):
    def test_unknown_repo_clear_is_escalated_not_merged(self) -> None:
        ticket = _ticket("randomuser/randomrepo", 1)
        clear = _clear(ticket, "randomuser/randomrepo")
        with patch("teatree.core.management.commands.ticket.merge_ticket_pr") as merge_pr:
            result = _merge(clear.pk)
        merge_pr.assert_not_called()
        assert result["merged"] is False
        assert result["escalated"] is True
        assert "randomuser/randomrepo" in result["error"]

    def test_unknown_repo_clear_merges_with_human_authorized(self) -> None:
        ticket = _ticket("randomuser/randomrepo", 2)
        clear = _clear(ticket, "randomuser/randomrepo")
        with patch("teatree.core.management.commands.ticket.merge_ticket_pr") as merge_pr:
            _merge(clear.pk, human_authorized="souliane")
        merge_pr.assert_called_once()

    def test_owned_repo_clear_is_not_held_by_the_scope_gate(self) -> None:
        ticket = _ticket("souliane/teatree", 3)
        clear = _clear(ticket, "souliane/teatree")
        with patch("teatree.core.management.commands.ticket.merge_ticket_pr") as merge_pr:
            _merge(clear.pk)
        merge_pr.assert_called_once()

    def test_unresolvable_host_fails_open(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        clear = _clear(ticket, "souliane/teatree")
        with patch("teatree.core.management.commands.ticket.merge_ticket_pr") as merge_pr:
            _merge(clear.pk)
        merge_pr.assert_called_once()
