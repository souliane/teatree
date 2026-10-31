"""The board holds a merge that leaves an issue item out (rules A and B) and unsettles what already settled (rule G).

Only the forge boundary is stubbed: the ``gh`` runner that answers a pull request's body and the
GitHub client's issue read. The FSM, the planning tasks and the rotation stamp are real.
"""

import contextlib
from collections.abc import Callable, Generator
from unittest.mock import MagicMock, patch

from django.test import TestCase

from teatree.backends import forge_merge_rpc
from teatree.backends.github import client as github_client
from teatree.core.backend_protocols import PrOpenState
from teatree.core.models import MergeClear, Task, Ticket
from teatree.loop.scanners import board_reconcile
from teatree.loop.scanners.board_reconcile import reconcile_board
from teatree.loop.scanners.board_reconcile_partial_merge import SETTLED_INSPECTION_CAP, settled_remainder_transitions
from teatree.loop.scanners.board_reconcile_report import BoardAction, BoardTransition
from tests.teatree_core.merge.partial_merge_helpers import merged_ticket, pr_bodies

_FIRST = "The first thing works"
_SECOND = "The second thing works"
_ISSUE = {"state": "open", "body": f"## Acceptance\n- {_FIRST}\n- {_SECOND}\n"}
_LEAVES_SECOND_OUT = f"Part of #4242\n\nDelivered:\n- {_FIRST}\n"
_DELIVERS_BOTH = f"Part of #4242\n\nDelivered:\n- {_FIRST}\n- {_SECOND}\n"


def _ticket(issue: int, pr: int, state: str) -> Ticket:
    ticket = merged_ticket(issue, pr)
    MergeClear.objects.filter(pr_id=pr).update(ticket=ticket)
    Ticket.objects.filter(pk=ticket.pk).update(state=state)
    ticket.refresh_from_db()
    return ticket


def _rule_g(*, dry_run: bool = False) -> list[BoardTransition]:
    transitions, _ = settled_remainder_transitions(
        overlay="", dry_run=dry_run, probe_budget=150, already_moved=frozenset()
    )
    return transitions


def _planning(ticket: Ticket) -> list[Task]:
    return list(Task.objects.filter(ticket=ticket, phase="planning", status=Task.Status.PENDING))


@contextlib.contextmanager
def _forge(bodies: dict[str, str], issue: Callable[[str], dict[str, str]] | Exception | None = None) -> Generator[None]:
    answer = issue or (lambda _url: _ISSUE)
    with (
        patch.object(forge_merge_rpc, "gh_runner", return_value=pr_bodies(bodies)),
        patch.object(
            github_client.GitHubCodeHost,
            "get_issue",
            side_effect=issue if isinstance(issue, Exception) else answer,
        ),
        patch.object(board_reconcile, "pr_open_state", lambda _url: PrOpenState.UNKNOWN),
    ):
        yield


class TestRuleAHoldsOnTheIssue(TestCase):
    def test_a_merged_pull_request_that_leaves_an_item_out_holds_the_ticket_and_plans_the_item(self) -> None:
        ticket = _ticket(4242, 859, Ticket.State.REVIEW_REQUESTED)

        with _forge({"859": _LEAVES_SECOND_OUT}):
            report = reconcile_board()

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.WORK_STARTED
        (task,) = _planning(ticket)
        assert _SECOND in task.execution_reason
        (row,) = report.transitions
        assert (row.action, row.applied, row.to_state) == (
            BoardAction.HELD_FOR_REMAINDER,
            True,
            Ticket.State.WORK_STARTED,
        )

    def test_a_merged_pull_request_that_quotes_every_item_settles_the_ticket(self) -> None:
        ticket = _ticket(4242, 859, Ticket.State.REVIEW_REQUESTED)

        with _forge({"859": _DELIVERS_BOTH}):
            report = reconcile_board()

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.MERGED
        assert [row.action for row in report.transitions] == [BoardAction.ADVANCED_MERGED]
        assert _planning(ticket) == []

    def test_a_dry_run_reports_the_hold_and_changes_nothing(self) -> None:
        ticket = _ticket(4242, 859, Ticket.State.REVIEW_REQUESTED)

        with _forge({"859": _LEAVES_SECOND_OUT}):
            report = reconcile_board(dry_run=True)

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.REVIEW_REQUESTED
        assert _planning(ticket) == []
        (row,) = report.transitions
        assert (row.action, row.applied) == (BoardAction.HELD_FOR_REMAINDER, False)

    def test_a_forge_that_does_not_answer_is_a_refused_row_and_the_ticket_stays(self) -> None:
        ticket = _ticket(4242, 859, Ticket.State.REVIEW_REQUESTED)

        with _forge({"859": _DELIVERS_BOTH}, issue=RuntimeError("forge down")):
            report = reconcile_board()

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.REVIEW_REQUESTED
        (row,) = report.refused
        assert row.error == "issue unreadable on the forge"

    def test_the_per_tick_pass_without_forge_probes_makes_no_forge_call_and_does_not_settle(self) -> None:
        ticket = _ticket(4242, 859, Ticket.State.REVIEW_REQUESTED)
        gh_runner, get_issue = MagicMock(), MagicMock()

        with (
            patch.object(forge_merge_rpc, "gh_runner", gh_runner),
            patch.object(github_client.GitHubCodeHost, "get_issue", get_issue),
        ):
            report = reconcile_board(probe_forge=False)

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.REVIEW_REQUESTED
        assert report.transitions == ()
        gh_runner.assert_not_called()
        get_issue.assert_not_called()

    def test_each_issue_linked_judgement_spends_one_probe_of_the_budget(self) -> None:
        first = _ticket(4242, 859, Ticket.State.REVIEW_REQUESTED)
        second = _ticket(4243, 860, Ticket.State.REVIEW_REQUESTED)

        with _forge({"859": _DELIVERS_BOTH, "860": _DELIVERS_BOTH.replace("4242", "4243")}):
            report = reconcile_board(probe_budget=1)

        states = {Ticket.objects.get(pk=ticket.pk).state for ticket in (first, second)}
        assert states == {Ticket.State.MERGED, Ticket.State.REVIEW_REQUESTED}
        assert report.probes == 1


class TestRuleGUnsettlesTheBacklog(TestCase):
    def test_a_settled_ticket_whose_last_merge_left_an_item_out_goes_back_to_planning_once(self) -> None:
        ticket = _ticket(4242, 859, Ticket.State.MERGED)

        with _forge({"859": _LEAVES_SECOND_OUT}):
            first = reconcile_board()
            second = reconcile_board()

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.WORK_STARTED
        (task,) = _planning(ticket)
        assert _SECOND in task.execution_reason
        assert [(row.action, row.applied) for row in first.transitions] == [(BoardAction.HELD_FOR_REMAINDER, True)]
        assert second.transitions == ()

    def test_tickets_that_need_no_unsettling_stay_where_they_are(self) -> None:
        closed = _ticket(4301, 901, Ticket.State.MERGED)
        closing = _ticket(4302, 902, Ticket.State.RETRO_RECORDED)
        umbrella = _ticket(4303, 903, Ticket.State.MERGED)
        delivered = _ticket(4304, 904, Ticket.State.DELIVERED)
        no_issue = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.MERGED, issue_url="")
        reviewer = _ticket(4305, 905, Ticket.State.MERGED)
        Ticket.objects.filter(pk=reviewer.pk).update(role=Ticket.Role.REVIEWER)

        def issue(url: str) -> dict[str, str]:
            if url.endswith("4301"):
                return {"state": "closed", "body": _ISSUE["body"]}
            if url.endswith("4303"):
                return {"state": "open", "body": f"# Never close this issue\n\n{_ISSUE['body']}"}
            return _ISSUE

        bodies = {
            "901": "Part of #4301\n",
            "902": "Closes #4302\n",
            "903": "Part of #4303\n",
            "904": "Part of #4304\n",
            "905": "Part of #4305\n",
        }
        with _forge(bodies, issue=issue):
            transitions = _rule_g()

        untouched = {
            ticket.pk: Ticket.objects.get(pk=ticket.pk).state
            for ticket in (closed, closing, umbrella, delivered, no_issue, reviewer)
        }
        assert untouched == {
            closed.pk: Ticket.State.MERGED,
            closing.pk: Ticket.State.RETRO_RECORDED,
            umbrella.pk: Ticket.State.MERGED,
            delivered.pk: Ticket.State.DELIVERED,
            no_issue.pk: Ticket.State.MERGED,
            reviewer.pk: Ticket.State.MERGED,
        }
        assert transitions == []

    def test_a_settled_ticket_the_forge_cannot_read_is_a_refused_row_and_stays_settled(self) -> None:
        ticket = _ticket(4242, 859, Ticket.State.MERGED)

        with _forge({"859": _LEAVES_SECOND_OUT}, issue=RuntimeError("forge down")):
            (row,) = _rule_g()

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.MERGED
        assert (row.action, row.error) == (BoardAction.REFUSED, "issue unreadable on the forge")

    def test_a_dry_run_lists_the_candidate_and_stamps_nothing(self) -> None:
        ticket = _ticket(4242, 859, Ticket.State.MERGED)

        with _forge({"859": _LEAVES_SECOND_OUT}):
            transitions = _rule_g(dry_run=True)

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.MERGED
        assert "settled_checked_at" not in ticket.extra
        assert [(row.action, row.applied) for row in transitions] == [(BoardAction.HELD_FOR_REMAINDER, False)]

    def test_the_per_tick_pass_without_forge_probes_leaves_settled_tickets_alone(self) -> None:
        ticket = _ticket(4242, 859, Ticket.State.MERGED)

        with _forge({"859": _LEAVES_SECOND_OUT}):
            reconcile_board(probe_forge=False)

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.MERGED

    def test_one_run_inspects_a_bounded_batch_and_the_next_run_starts_with_the_unchecked(self) -> None:
        total = SETTLED_INSPECTION_CAP + 5
        tickets = [_ticket(5000 + n, 7000 + n, Ticket.State.MERGED) for n in range(total)]
        bodies = {str(7000 + n): f"Closes #{5000 + n}\n" for n in range(total)}

        def checked() -> int:
            return sum("settled_checked_at" in Ticket.objects.get(pk=ticket.pk).extra for ticket in tickets)

        with _forge(bodies):
            _rule_g()
            after_first = checked()
            _rule_g()
            after_second = checked()

        assert (after_first, after_second) == (SETTLED_INSPECTION_CAP, total)
