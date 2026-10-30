import json
from collections.abc import Callable
from datetime import timedelta

from django.utils import timezone

from teatree.core.models import MergeAudit, MergeClear, PullRequest, Ticket

type GhRunner = Callable[[list[str]], tuple[int, str, str]]


def merged_pr(ticket: Ticket, number: int, *, minutes_ago: int) -> None:
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


def merged_ticket(issue: int, pr: int, *, overlay: str = "t3-teatree") -> Ticket:
    ticket = Ticket.objects.create(
        overlay=overlay,
        issue_url=f"https://github.com/souliane/teatree/issues/{issue}",
        state=Ticket.State.MERGED,
    )
    merged_pr(ticket, pr, minutes_ago=10)
    return ticket


def pr_bodies(bodies: dict[str, str]) -> GhRunner:
    def run(argv: list[str]) -> tuple[int, str, str]:
        return 0, json.dumps({"title": "A change", "body": bodies[argv[2]]}), ""

    return run
