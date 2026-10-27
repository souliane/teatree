import json
from datetime import timedelta
from io import StringIO
from typing import cast
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from teatree.backends import forge_merge_rpc
from teatree.backends.github import client as github_client
from teatree.core.models import MergeAudit, MergeClear, PullRequest, Ticket


def _merged_pr(ticket: Ticket, number: int, *, minutes_ago: int) -> None:
    PullRequest.objects.create(
        ticket=ticket,
        overlay="t3-teatree",
        url=f"https://github.com/souliane/teatree/pull/{number}",
        repo="souliane/teatree",
        iid=str(number),
        state=PullRequest.State.MERGED,
    )
    clear = MergeClear.objects.create(
        pr_id=number,
        slug="souliane/teatree",
        host_kind="github",
        reviewed_sha="a" * 40,
        reviewer_identity="independent-reviewer",
        gh_verify_result=MergeClear.VerifyResult.GREEN,
        blast_class=MergeClear.BlastClass.LOGIC,
    )
    MergeAudit.objects.create(
        clear=clear,
        merged_sha="b" * 40,
        required_checks_status="green",
        repo_slug="souliane/teatree",
        merged_at=timezone.now() - timedelta(minutes=minutes_ago),
    )


class TestPartialMergeReport(TestCase):
    def test_lists_open_merged_ticket_when_its_latest_merge_declares_a_remainder(self) -> None:
        ticket = Ticket.objects.create(
            overlay="t3-teatree",
            issue_url="https://github.com/souliane/teatree/issues/4242",
            state=Ticket.State.MERGED,
        )
        _merged_pr(ticket, 859, minutes_ago=20)
        _merged_pr(ticket, 860, minutes_ago=10)

        def run(argv: list[str]) -> tuple[int, str, str]:
            body = "Closes #4242" if argv[2] == "859" else "Part of #4242\n\nUnmet: finish the report"
            return 0, json.dumps({"title": "A change", "body": body}), ""

        with (
            patch.object(forge_merge_rpc, "gh_runner", return_value=run),
            patch.object(github_client.GitHubCodeHost, "get_issue", return_value={"state": "open", "body": ""}),
        ):
            output = StringIO()
            result = cast(
                "dict[str, list[dict[str, object]]]",
                call_command("ticket", "partial-merge-report", "--json", stdout=output),
            )

        assert result["matches"] == [
            {
                "ticket_id": ticket.pk,
                "issue_url": ticket.issue_url,
                "pr_url": "https://github.com/souliane/teatree/pull/860",
                "unmet": ["finish the report"],
            }
        ]
        assert result["unreadable"] == []
        assert json.loads(output.getvalue()) == result

    def test_closed_issue_and_completed_latest_merge_are_excluded(self) -> None:
        closed = Ticket.objects.create(
            overlay="t3-teatree",
            issue_url="https://github.com/souliane/teatree/issues/4242",
            state=Ticket.State.MERGED,
        )
        _merged_pr(closed, 859, minutes_ago=20)
        completed = Ticket.objects.create(
            overlay="t3-teatree",
            issue_url="https://github.com/souliane/teatree/issues/4243",
            state=Ticket.State.MERGED,
        )
        _merged_pr(completed, 860, minutes_ago=20)
        _merged_pr(completed, 861, minutes_ago=10)

        def run(argv: list[str]) -> tuple[int, str, str]:
            body = "Closes #4243" if argv[2] == "861" else "Part of #4242\n\nUnmet: finish the report"
            return 0, json.dumps({"title": "A change", "body": body}), ""

        def issue(url: str) -> dict[str, str]:
            return {"state": "closed" if url.endswith("4242") else "open", "body": ""}

        with (
            patch.object(forge_merge_rpc, "gh_runner", return_value=run),
            patch.object(github_client.GitHubCodeHost, "get_issue", side_effect=issue),
        ):
            result = cast("dict[str, list[dict[str, object]]]", call_command("ticket", "partial-merge-report"))

        assert result == {"matches": [], "unreadable": []}

    def test_an_unordered_last_merge_is_reported_as_unreadable(self) -> None:
        ticket = Ticket.objects.create(
            overlay="t3-teatree",
            issue_url="https://github.com/souliane/teatree/issues/4242",
            state=Ticket.State.MERGED,
        )
        _merged_pr(ticket, 859, minutes_ago=20)
        PullRequest.objects.create(
            ticket=ticket,
            overlay="t3-teatree",
            url="https://github.com/souliane/teatree/pull/860",
            repo="souliane/teatree",
            iid="860",
            state=PullRequest.State.MERGED,
        )

        result = cast("dict[str, list[dict[str, object]]]", call_command("ticket", "partial-merge-report"))

        assert result == {
            "matches": [],
            "unreadable": [{"ticket_id": ticket.pk, "reason": "last merged pull request unknown"}],
        }
