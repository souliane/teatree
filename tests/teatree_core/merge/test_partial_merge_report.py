import json
from datetime import timedelta
from io import StringIO
from typing import cast
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from teatree.backends import forge_merge_rpc
from teatree.backends.errors import IssueNotFoundError
from teatree.backends.github import client as github_client
from teatree.core.models import MergeAudit, MergeClear, PullRequest, Ticket
from teatree.utils.run import CommandFailedError


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
    def test_reports_issue_boxes_when_the_latest_partial_body_has_no_marker(self) -> None:
        ticket = Ticket.objects.create(
            overlay="t3-teatree",
            issue_url="https://github.com/souliane/teatree/issues/4242",
            state=Ticket.State.MERGED,
        )
        _merged_pr(ticket, 859, minutes_ago=10)

        def run(argv: list[str]) -> tuple[int, str, str]:
            return 0, json.dumps({"title": "A change", "body": "Part of #4242"}), ""

        with (
            patch.object(forge_merge_rpc, "gh_runner", return_value=run),
            patch.object(
                github_client.GitHubCodeHost,
                "get_issue",
                return_value={"state": "open", "body": "## Acceptance\n- [ ] finish the report"},
            ),
        ):
            result = cast(
                "dict[str, list[dict[str, object]]]", call_command("ticket", "partial-merge-report", "--json")
            )

        assert result["matches"][0]["unmet"] == ["finish the report"]

    def test_reports_unreadable_issue_state_and_pull_request_body(self) -> None:
        unknown_state = Ticket.objects.create(
            overlay="t3-teatree",
            issue_url="https://github.com/souliane/teatree/issues/4242",
            state=Ticket.State.MERGED,
        )
        _merged_pr(unknown_state, 859, minutes_ago=20)
        missing_body = Ticket.objects.create(
            overlay="t3-teatree",
            issue_url="https://github.com/souliane/teatree/issues/4243",
            state=Ticket.State.MERGED,
        )
        _merged_pr(missing_body, 860, minutes_ago=10)

        def run(argv: list[str]) -> tuple[int, str, str]:
            return 1, "", "unreadable"

        def issue(url: str) -> dict[str, str]:
            return {"state": "unknown" if url.endswith("4242") else "open", "body": ""}

        with (
            patch.object(forge_merge_rpc, "gh_runner", return_value=run),
            patch.object(github_client.GitHubCodeHost, "get_issue", side_effect=issue),
        ):
            result = cast(
                "dict[str, list[dict[str, object]]]", call_command("ticket", "partial-merge-report", "--json")
            )

        assert result["unreadable"] == [
            {"ticket_id": unknown_state.pk, "reason": "issue state unreadable"},
            {"ticket_id": missing_body.pk, "reason": "pull request body unreadable"},
        ]

    def test_reports_a_pull_request_read_exception_as_unreadable(self) -> None:
        ticket = Ticket.objects.create(
            overlay="t3-teatree",
            issue_url="https://github.com/souliane/teatree/issues/4242",
            state=Ticket.State.MERGED,
        )
        _merged_pr(ticket, 859, minutes_ago=10)

        with (
            patch.object(github_client.GitHubCodeHost, "get_issue", return_value={"state": "open", "body": ""}),
            patch.object(github_client.GitHubCodeHost, "fetch_pr_message", side_effect=RuntimeError("forge down")),
        ):
            result = cast(
                "dict[str, list[dict[str, object]]]", call_command("ticket", "partial-merge-report", "--json")
            )

        assert result["unreadable"] == [{"ticket_id": ticket.pk, "reason": "pull request body unreadable"}]

    def test_overlay_filter_and_human_output_show_only_selected_ticket(self) -> None:
        selected = Ticket.objects.create(
            overlay="t3-teatree",
            issue_url="https://github.com/souliane/teatree/issues/4242",
            state=Ticket.State.MERGED,
        )
        _merged_pr(selected, 859, minutes_ago=10)
        other = Ticket.objects.create(
            overlay="other", issue_url="https://github.com/souliane/teatree/issues/4243", state=Ticket.State.MERGED
        )
        _merged_pr(other, 860, minutes_ago=5)

        def run(argv: list[str]) -> tuple[int, str, str]:
            return 0, json.dumps({"title": "A change", "body": "Part of #4242\n\nUnmet: finish the report"}), ""

        with (
            patch.object(forge_merge_rpc, "gh_runner", return_value=run),
            patch.object(github_client.GitHubCodeHost, "get_issue", return_value={"state": "open", "body": ""}),
        ):
            output = StringIO()
            result = cast(
                "dict[str, list[dict[str, object]]]",
                call_command("ticket", "partial-merge-report", "--overlay", "t3-teatree", stderr=output),
            )

        assert [row["ticket_id"] for row in result["matches"]] == [selected.pk]
        assert f"ticket {selected.pk}" in output.getvalue()
        assert "finish the report" in output.getvalue()
        assert f"ticket {other.pk}" not in output.getvalue()

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

    def test_a_forge_error_on_one_ticket_is_one_unreadable_row_not_a_failed_report(self) -> None:
        missing = Ticket.objects.create(
            overlay="t3-teatree",
            issue_url="https://github.com/souliane/teatree/issues/4241",
            state=Ticket.State.MERGED,
        )
        _merged_pr(missing, 858, minutes_ago=30)
        failing = Ticket.objects.create(
            overlay="t3-teatree",
            issue_url="https://github.com/souliane/teatree/issues/4242",
            state=Ticket.State.MERGED,
        )
        _merged_pr(failing, 859, minutes_ago=20)
        partial = Ticket.objects.create(
            overlay="t3-teatree",
            issue_url="https://github.com/souliane/teatree/issues/4243",
            state=Ticket.State.MERGED,
        )
        _merged_pr(partial, 860, minutes_ago=10)

        def run(argv: list[str]) -> tuple[int, str, str]:
            return 0, json.dumps({"title": "A change", "body": "Part of #4243\n\nUnmet: finish the report"}), ""

        def issue(url: str) -> dict[str, str]:
            if url.endswith("4241"):
                raise IssueNotFoundError(url)
            if url.endswith("4242"):
                raise CommandFailedError(["gh", "issue", "view"], 1, "", "boom")
            return {"state": "open", "body": ""}

        with (
            patch.object(forge_merge_rpc, "gh_runner", return_value=run),
            patch.object(github_client.GitHubCodeHost, "get_issue", side_effect=issue),
        ):
            result = cast("dict[str, list[dict[str, object]]]", call_command("ticket", "partial-merge-report"))

        assert result["unreadable"] == [
            {"ticket_id": missing.pk, "reason": "issue unreadable on the forge"},
            {"ticket_id": failing.pk, "reason": "issue unreadable on the forge"},
        ]
        assert [row["ticket_id"] for row in result["matches"]] == [partial.pk]
