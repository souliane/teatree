"""The board's merge-refusal issue closes when a ticket LANDS, by any path, and on no other save.

The refusal is pinned against auto-resolve, so the landing itself is the only thing that clears it.
Two shapes reach a landed state: an FSM transition followed by a save (the board, the keystone, the
CLI) and the tracker syncs' bulk ``merge_extra(also_set={"state": ...})``, which fires no signal at
all. Both must close it exactly once; an unrelated save of an already-landed ticket must not even
look at it — on the write-serialised production SQLite that look is a second write-lock round-trip.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.db import OperationalError, connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from teatree.backends.github import ProjectItem
from teatree.backends.github.sync import GitHubSyncBackend
from teatree.backends.gitlab.sync_terminal import apply_merged_status
from teatree.core.backend_protocols import PrMergeState
from teatree.core.gates import merge_evidence_gate
from teatree.core.models import KnownIssue, PullRequest, Ticket
from teatree.core.models.known_issue import merge_refusal_fingerprint
from teatree.forge_credentials import ForgeTokenResolution, ForgeTokenState
from teatree.types import SyncResult
from tests.teatree_core.sync._overlays import SyncOverlay, _patch_overlay

_ISSUE_URL = "https://github.com/souliane/teatree/issues/61"


def _refused(state: str = Ticket.State.REVIEW_REQUESTED) -> tuple[Ticket, KnownIssue]:
    ticket = Ticket.objects.create(overlay="test", issue_url=_ISSUE_URL, state=state)
    issue = KnownIssue.objects.create(
        fingerprint=merge_refusal_fingerprint(ticket.pk), summary="board cannot confirm the merge", auto_resolve=False
    )
    return ticket, issue


def _landed_on_the_forge(ticket: Ticket) -> None:
    """The merge evidence ``mark_merged`` demands: a merged PR row the forge confirms."""
    PullRequest.objects.create(
        ticket=ticket,
        url=f"https://github.com/souliane/teatree/pull/{ticket.pk}",
        repo="souliane/teatree",
        iid=str(ticket.pk),
        overlay=ticket.overlay,
        state=PullRequest.State.MERGED,
    )
    merged = SimpleNamespace(pr_merge_state=lambda: PrMergeState(state="MERGED", merge_commit_oid="a" * 40))
    with patch("teatree.core.merge.ci_rollup.CodeHostQuery.for_ref", return_value=merged):
        assert merge_evidence_gate.record_confirmed_forge_merge(ticket)


def _known_issue_queries(captured: CaptureQueriesContext) -> list[str]:
    return [query["sql"] for query in captured.captured_queries if KnownIssue._meta.db_table in query["sql"]]


class TestAnFsmLandingClosesTheRefusalOnce(TestCase):
    def test_an_unrelated_save_of_a_merged_ticket_never_touches_the_refusal(self) -> None:
        ticket, issue = _refused(Ticket.State.MERGED)

        with CaptureQueriesContext(connection) as captured:
            ticket.save(update_fields=["extra"])
            ticket.save()

        assert _known_issue_queries(captured) == []
        issue.refresh_from_db()
        assert issue.is_open

    def test_the_save_that_lands_the_merge_closes_it_and_a_later_save_does_not_look_again(self) -> None:
        ticket, issue = _refused()
        _landed_on_the_forge(ticket)

        ticket.mark_merged()
        ticket.save()
        issue.refresh_from_db()
        assert not issue.is_open

        with CaptureQueriesContext(connection) as captured:
            ticket.save()
        assert _known_issue_queries(captured) == []

    def test_a_move_between_landed_states_never_touches_the_refusal(self) -> None:
        ticket, issue = _refused(Ticket.State.MERGED)

        with CaptureQueriesContext(connection) as captured:
            ticket.retrospect()
            ticket.save()
            ticket.retrospect()
            ticket.save()

        assert ticket.state == Ticket.State.RETRO_RECORDED
        assert _known_issue_queries(captured) == []
        issue.refresh_from_db()
        assert issue.is_open

    def test_a_landing_whose_save_failed_closes_it_on_the_save_that_persists_it(self) -> None:
        ticket, issue = _refused()
        _landed_on_the_forge(ticket)
        ticket.mark_merged()

        with (
            patch.object(Ticket, "save_base", side_effect=OperationalError("database is locked")),
            pytest.raises(OperationalError),
        ):
            ticket.save()
        issue.refresh_from_db()
        assert issue.is_open

        ticket.save()
        issue.refresh_from_db()
        assert not issue.is_open

    def test_a_save_that_leaves_the_landed_state_unwritten_keeps_it_open(self) -> None:
        ticket, issue = _refused()
        _landed_on_the_forge(ticket)
        ticket.mark_merged()

        ticket.save(update_fields=["extra"])
        issue.refresh_from_db()
        assert issue.is_open

        ticket.save(update_fields=["state"])
        issue.refresh_from_db()
        assert not issue.is_open


class TestABulkSyncLandingClosesTheRefusal(TestCase):
    def test_a_merge_extra_that_lands_the_ticket_closes_it(self) -> None:
        ticket, issue = _refused()

        ticket.merge_extra(also_set={"state": Ticket.State.MERGED})

        issue.refresh_from_db()
        assert not issue.is_open

    def test_a_merge_extra_that_does_not_land_the_ticket_never_touches_it(self) -> None:
        ticket, issue = _refused(Ticket.State.PR_OPENED)

        with CaptureQueriesContext(connection) as captured:
            ticket.merge_extra(also_set={"state": Ticket.State.REVIEW_REQUESTED})

        assert _known_issue_queries(captured) == []
        issue.refresh_from_db()
        assert issue.is_open

    def test_a_merge_extra_on_an_already_landed_ticket_never_touches_it(self) -> None:
        ticket, issue = _refused(Ticket.State.MERGED)

        with CaptureQueriesContext(connection) as captured:
            ticket.merge_extra(set_keys={"prs": {}}, also_set={"state": Ticket.State.DELIVERED})

        assert _known_issue_queries(captured) == []
        issue.refresh_from_db()
        assert issue.is_open

    def test_the_gitlab_merged_status_sync_closes_it(self) -> None:
        ticket, issue = _refused()
        ticket.merge_extra(set_keys={"prs": {"url1": {"title": "MR1"}}})

        with patch("teatree.backends.gitlab.sync_terminal.cleanup_worktree"):
            apply_merged_status(ticket, {"url1"}, SyncResult())

        assert Ticket.objects.get(pk=ticket.pk).state == Ticket.State.MERGED
        issue.refresh_from_db()
        assert not issue.is_open

    def test_the_github_board_done_column_closes_it(self) -> None:
        ticket, issue = _refused()
        overlay = SyncOverlay(
            gitlab_token="",
            gitlab_username="",
            github_token="gh-test-token",
            github_owner="souliane",
            github_project_number=1,
        )
        done = ProjectItem(
            issue_number=61,
            title="Board item",
            url=_ISSUE_URL,
            status="Done",
            position=1,
            labels=[],
            updated_at="2026-05-01T00:00:00Z",
        )

        with (
            _patch_overlay(overlay),
            patch("teatree.backends.github.fetch_project_items", return_value=[done]),
            patch.object(GitHubSyncBackend, "_sync_reviewer_prs"),
            patch("teatree.backends.github.sync.cleanup_worktree"),
            patch(
                "teatree.forge_credentials.resolve_overlay_token",
                return_value=ForgeTokenResolution("github_token", "test", ForgeTokenState.TOKEN, token="gh-test-token"),
            ),
        ):
            result = GitHubSyncBackend().sync(overlay)

        assert result.errors == []
        assert Ticket.objects.get(pk=ticket.pk).state == Ticket.State.DELIVERED
        issue.refresh_from_db()
        assert not issue.is_open
