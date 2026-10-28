import json
from collections.abc import Callable
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


type _Report = dict[str, list[dict[str, object]]]
type _GhRunner = Callable[[list[str]], tuple[int, str, str]]


def _merged_ticket(issue: int, pr: int, *, overlay: str = "t3-teatree") -> Ticket:
    ticket = Ticket.objects.create(
        overlay=overlay,
        issue_url=f"https://github.com/souliane/teatree/issues/{issue}",
        state=Ticket.State.MERGED,
    )
    _merged_pr(ticket, pr, minutes_ago=10)
    return ticket


def _part_of(issue: int) -> str:
    return f"Part of #{issue}\n\nUnmet: finish the report\nRemaining after this PR: wire the docs\n"


def _pr_bodies(bodies: dict[str, str]) -> _GhRunner:
    def run(argv: list[str]) -> tuple[int, str, str]:
        return 0, json.dumps({"title": "A change", "body": bodies[argv[2]]}), ""

    return run


def _report(
    *args: str, gh: _GhRunner, issue: Callable[[str], dict[str, str]], stderr: StringIO | None = None
) -> _Report:
    with (
        patch.object(forge_merge_rpc, "gh_runner", return_value=gh),
        patch.object(github_client.GitHubCodeHost, "get_issue", side_effect=issue),
    ):
        return cast("_Report", call_command("ticket", "partial-merge-report", *args, stderr=stderr))


def _open_issue(url: str) -> dict[str, str]:
    return {"state": "open", "body": ""}


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

    def test_an_issue_the_forge_returns_without_a_known_state_is_one_unreadable_row(self) -> None:
        no_state = _merged_ticket(4242, 859)
        odd_state = _merged_ticket(4243, 860)

        def issue(url: str) -> dict[str, str]:
            return {"body": ""} if url.endswith("4242") else {"state": "draft", "body": ""}

        result = _report(gh=_pr_bodies({"859": _part_of(4242), "860": _part_of(4243)}), issue=issue)

        assert result == {
            "matches": [],
            "unreadable": [
                {"ticket_id": no_state.pk, "reason": "issue state unreadable"},
                {"ticket_id": odd_state.pk, "reason": "issue state unreadable"},
            ],
        }

    def test_a_pull_request_body_the_forge_does_not_return_is_one_unreadable_row(self) -> None:
        silent = _merged_ticket(4242, 859)
        readable = _merged_ticket(4243, 860)
        readable_body = _pr_bodies({"860": _part_of(4243)})

        def gh(argv: list[str]) -> tuple[int, str, str]:
            return (1, "", "HTTP 502") if argv[2] == "859" else readable_body(argv)

        result = _report(gh=gh, issue=_open_issue)

        assert result["unreadable"] == [{"ticket_id": silent.pk, "reason": "pull request body unreadable"}]
        assert [row["ticket_id"] for row in result["matches"]] == [readable.pk]

    def test_the_overlay_option_limits_the_report_to_that_overlays_tickets(self) -> None:
        _merged_ticket(4242, 859)
        other = _merged_ticket(4243, 860, overlay="other-overlay")

        result = _report(
            "--overlay",
            "other-overlay",
            gh=_pr_bodies({"859": _part_of(4242), "860": _part_of(4243)}),
            issue=_open_issue,
        )

        assert [row["ticket_id"] for row in result["matches"]] == [other.pk]
        assert result["unreadable"] == []

    def test_the_human_output_lists_matches_then_unreadable_tickets(self) -> None:
        unreadable = _merged_ticket(4242, 859)
        partial = _merged_ticket(4243, 860)
        stderr = StringIO()

        def issue(url: str) -> dict[str, str]:
            return {"body": ""} if url.endswith("4242") else {"state": "open", "body": ""}

        _report(gh=_pr_bodies({"860": _part_of(4243)}), issue=issue, stderr=stderr)

        pr_url = "https://github.com/souliane/teatree/pull/860"
        assert stderr.getvalue().splitlines() == [
            f"ticket {partial.pk} {partial.issue_url} — {pr_url}: finish the report, wire the docs",
            f"ticket {unreadable.pk}: unreadable (issue state unreadable)",
        ]

    def test_the_human_output_says_so_when_no_merged_ticket_declares_a_remainder(self) -> None:
        stderr = StringIO()

        _report(gh=_pr_bodies({}), issue=_open_issue, stderr=stderr)

        assert stderr.getvalue().strip() == "no merged tickets with a declared remainder"
