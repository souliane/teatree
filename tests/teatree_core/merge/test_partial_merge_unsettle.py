from typing import cast
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

from teatree.backends import forge_merge_rpc
from teatree.backends.github import client as github_client
from teatree.core.models import Task, Ticket
from tests.teatree_core.merge.test_partial_merge_report import _merged_ticket, _pr_bodies


def _unsettle(
    ticket: Ticket,
    *,
    body: str,
    issue_data: dict[str, str] | None = None,
    reason: str = "",
    dry_run: bool = False,
) -> dict[str, object]:
    gh = _pr_bodies({"860": body, "861": body})
    args = ["ticket", "unsettle", str(ticket.pk)]
    if reason:
        args.extend(["--reason", reason])
    if dry_run:
        args.append("--dry-run")

    def issue(_url: str) -> dict[str, str]:
        return issue_data or {"state": "open", "body": ""}

    with (
        patch.object(forge_merge_rpc, "gh_runner", return_value=gh),
        patch.object(github_client.GitHubCodeHost, "get_issue", side_effect=issue),
    ):
        return cast("dict[str, object]", call_command(*args))


class TestPartialMergeUnsettle(TestCase):
    def test_refuses_unknown_ticket_and_ticket_without_forge_issue_url(self) -> None:
        no_url = Ticket.objects.create(overlay="t3-teatree", issue_url="auto:missing", state=Ticket.State.MERGED)

        missing = cast("dict[str, object]", call_command("ticket", "unsettle", "999999"))
        synthetic = cast("dict[str, object]", call_command("ticket", "unsettle", str(no_url.pk)))

        assert "not found" in cast("str", missing["error"])
        assert synthetic["error"] == "ticket has no forge issue URL"
        assert not Task.objects.filter(ticket=no_url).exists()

    def test_refuses_closed_issue_without_changing_ticket_or_queuing_task(self) -> None:
        ticket = _merged_ticket(4242, 860)

        result = _unsettle(
            ticket, body="Part of #4242", issue_data={"state": "closed", "body": ""}, reason="Finish work"
        )

        ticket.refresh_from_db()
        assert "error" in result
        assert ticket.state == Ticket.State.MERGED
        assert not Task.objects.filter(ticket=ticket).exists()

    def test_refuses_closing_keyword_even_when_issue_stays_open(self) -> None:
        ticket = _merged_ticket(4242, 860)

        result = _unsettle(ticket, body="Closes #4242", reason="Finish work")

        ticket.refresh_from_db()
        assert "error" in result
        assert ticket.state == Ticket.State.MERGED
        assert not Task.objects.filter(ticket=ticket).exists()

    def test_reopens_settled_ticket_and_queues_one_reasoned_plan(self) -> None:
        ticket = _merged_ticket(4242, 860)
        ticket.extra = {"pr_url_by_branch": {"old": "https://github.com/souliane/teatree/pull/860"}}
        ticket.save(update_fields=["extra"])

        result = _unsettle(ticket, body="Part of #4242\n\nUnmet: finish audit")

        ticket.refresh_from_db()
        plans = list(Task.objects.filter(ticket=ticket, phase="planning"))
        assert result["planning_task"] == plans[0].pk
        assert ticket.state == Ticket.State.WORK_STARTED
        assert len(plans) == 1
        assert "finish audit" in plans[0].execution_reason
        assert ticket.extra["reopened_from"] == Ticket.State.MERGED
        assert ticket.extra["reopened_over_pr_urls"] == ["https://github.com/souliane/teatree/pull/860"]
        assert "pr_url_by_branch" not in ticket.extra

        second = _unsettle(ticket, body="Part of #4242\n\nUnmet: finish audit")
        assert "error" in second
        assert Task.objects.filter(ticket=ticket, phase="planning").count() == 1

    def test_reason_can_authorize_part_of_without_declared_remainder_but_dry_run_writes_nothing(self) -> None:
        ticket = _merged_ticket(4242, 860)

        refused = _unsettle(ticket, body="Part of #4242")
        preview = _unsettle(ticket, body="Part of #4242", reason="Finish the audit", dry_run=True)

        ticket.refresh_from_db()
        assert "error" in refused
        assert preview["dry_run"] is True
        assert ticket.state == Ticket.State.MERGED
        assert not Task.objects.filter(ticket=ticket).exists()

        applied = _unsettle(ticket, body="Part of #4242", reason="Finish the audit")
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.WORK_STARTED
        assert "Finish the audit" in Task.objects.get(pk=applied["planning_task"]).execution_reason

    def test_reopens_delivered_ticket_with_a_reason_when_pr_has_no_issue_link(self) -> None:
        ticket = _merged_ticket(4242, 860)
        ticket.state = Ticket.State.DELIVERED
        ticket.save(update_fields=["state"])

        result = _unsettle(ticket, body="Independent change", reason="Finish the audit")

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.WORK_STARTED
        assert ticket.extra["reopened_from"] == Ticket.State.DELIVERED
        assert Task.objects.get(pk=result["planning_task"]).phase == "planning"

    def test_refuses_reviewer_ticket_and_umbrella_issue(self) -> None:
        reviewer = _merged_ticket(4242, 860)
        reviewer.role = Ticket.Role.REVIEWER
        reviewer.save(update_fields=["role"])
        umbrella = _merged_ticket(4243, 861)

        reviewer_result = _unsettle(reviewer, body="Part of #4242", reason="Finish work")
        umbrella_result = _unsettle(
            umbrella,
            body="Part of #4243",
            issue_data={"state": "open", "body": "## Do not close this issue\n\nA standing backlog."},
            reason="Finish work",
        )

        assert "error" in reviewer_result
        assert "umbrella issue" in cast("str", umbrella_result["error"])
        assert not Task.objects.filter(ticket__in=[reviewer, umbrella]).exists()

    def test_refuses_unreadable_issue_and_pull_request_body(self) -> None:
        ticket = _merged_ticket(4242, 860)

        unknown_issue = _unsettle(ticket, body="Part of #4242", issue_data={"state": "draft", "body": ""})
        with (
            patch.object(forge_merge_rpc, "gh_runner", return_value=lambda _argv: (1, "", "HTTP 502")),
            patch.object(github_client.GitHubCodeHost, "get_issue", return_value={"state": "open", "body": ""}),
        ):
            unknown_pr = cast("dict[str, object]", call_command("ticket", "unsettle", str(ticket.pk)))

        ticket.refresh_from_db()
        assert unknown_issue["error"] == "issue state unreadable"
        assert unknown_pr["error"] == "pull request body unreadable"
        assert ticket.state == Ticket.State.MERGED
        assert not Task.objects.filter(ticket=ticket).exists()

    def test_refuses_nonsettled_ticket_before_asking_forge(self) -> None:
        ticket = _merged_ticket(4242, 860)
        ticket.state = Ticket.State.WORK_STARTED
        ticket.save(update_fields=["state"])

        with patch.object(github_client.GitHubCodeHost, "get_issue") as get_issue:
            result = cast(
                "dict[str, object]", call_command("ticket", "unsettle", str(ticket.pk), "--reason", "Finish work")
            )

        assert "error" in result
        get_issue.assert_not_called()
        assert not Task.objects.filter(ticket=ticket).exists()

    def test_refuses_when_ticket_moves_out_of_settled_state_during_forge_read(self) -> None:
        ticket = _merged_ticket(4242, 860)

        def advance(_url: str) -> dict[str, str]:
            Ticket.objects.filter(pk=ticket.pk).update(state=Ticket.State.WORK_STARTED)
            return {"state": "open", "body": ""}

        with (
            patch.object(forge_merge_rpc, "gh_runner", return_value=_pr_bodies({"860": "Part of #4242\nUnmet: audit"})),
            patch.object(github_client.GitHubCodeHost, "get_issue", side_effect=advance),
        ):
            result = cast("dict[str, object]", call_command("ticket", "unsettle", str(ticket.pk)))

        ticket.refresh_from_db()
        assert "error" in result
        assert ticket.state == Ticket.State.WORK_STARTED
        assert not Task.objects.filter(ticket=ticket).exists()
