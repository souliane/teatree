"""The planning survey's bounded listing of defects and idle author tickets."""

import json
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from teatree.core.intake.fix_first import FixFirst
from teatree.core.intake.landscape_gather import run_landscape
from teatree.core.modelkit.task_failure_taxonomy import FailureKind
from teatree.core.models import PullRequest, Task, Ticket
from teatree.core.models.shipping_hold import is_shipping_held
from teatree.core.models.transition import TicketTransition
from teatree.loop.stuck_ticket_redispatch import idle_started_author_tickets
from teatree.types import RawAPIDict
from tests.factories import PullRequestFactory, TaskFactory

_FACTORY = "teatree.core.intake.landscape_gather.code_host_from_overlay"
_OVERLAY = "teatree.core.intake.landscape_gather.get_overlay"


class _Host:
    def __init__(self, issues: list[RawAPIDict], *, fail: bool = False) -> None:
        self.issues = issues
        self.fail = fail

    def current_user(self) -> str:
        return "me"

    def list_my_prs(self, *, author: str, updated_after: str | None = None) -> list[RawAPIDict]:
        return []

    def list_my_merged_prs(self, *, author: str, updated_after: str | None = None) -> list[RawAPIDict]:
        return []

    def list_assigned_issues(self, *, assignee: str) -> list[RawAPIDict]:
        return []

    def list_repo_open_issues(self, *, repo: str) -> list[RawAPIDict]:
        if self.fail:
            msg = "forge unavailable"
            raise RuntimeError(msg)
        return self.issues


class _Review:
    @staticmethod
    def merge_candidate_repo_slugs() -> list[str]:
        return ["owner/repo"]


class _Overlay:
    review = _Review()


def _issue(index: int, title: str, labels: list[str] | None = None) -> RawAPIDict:
    return {
        "url": f"https://github.com/owner/repo/issues/{index}",
        "title": title,
        "labels": labels or [],
        "created_at": f"2026-01-{index:02d}T00:00:00Z",
    }


def _aged_ticket(*, state: str, hours: int = 48) -> Ticket:
    ticket = Ticket.objects.create(
        role=Ticket.Role.AUTHOR, state=state, issue_url=f"https://example.org/{state}/{Ticket.objects.count()}"
    )
    transition = TicketTransition.objects.create(ticket=ticket, from_state="scoped", to_state=state)
    TicketTransition.objects.filter(pk=transition.pk).update(created_at=timezone.now() - timedelta(hours=hours))
    return ticket


class TestFixFirstLandscape(TestCase):
    def _report(self, issues: list[RawAPIDict], *, fail: bool = False) -> FixFirst:
        with patch(_FACTORY, return_value=_Host(issues, fail=fail)), patch(_OVERLAY, return_value=_Overlay()):
            return run_landscape(Path("/does-not-exist"))["fix_first"]

    def test_defects_are_oldest_first_capped_and_exclude_nits_and_features(self) -> None:
        issues = [_issue(i, f"fix: fault {i}") for i in range(7, 0, -1)]
        issues.extend([_issue(8, "feature: new"), _issue(9, "fix: polish", ["nit"])])
        dateless = _issue(10, "fix: undated")
        del dateless["created_at"]
        issues.append(dateless)
        issues[0]["title"] = "ordinary title"
        issues[0]["labels"] = ["bug"]

        result = self._report(issues)

        assert [item["issue"] for item in result["defects"]] == [
            f"https://github.com/owner/repo/issues/{i}" for i in range(1, 6)
        ]
        assert result["more_defects"] == 3
        assert result["unread"] == []

    def test_stuck_matches_author_sweep_state_and_exclusions(self) -> None:
        expected = {
            Ticket.State.WORK_STARTED,
            Ticket.State.PLAN_RECORDED,
            Ticket.State.CODED,
            Ticket.State.TESTED,
            Ticket.State.SELF_REVIEWED,
        }
        tickets = {state: _aged_ticket(state=state) for state in Ticket.State.values}
        recent = _aged_ticket(state=Ticket.State.WORK_STARTED, hours=0)
        active = _aged_ticket(state=Ticket.State.WORK_STARTED)
        TaskFactory(ticket=active, status=Task.Status.PENDING)
        with_pr = _aged_ticket(state=Ticket.State.WORK_STARTED)
        PullRequestFactory(ticket=with_pr, state=PullRequest.State.REVIEW_REQUESTED)
        held = _aged_ticket(state=Ticket.State.SELF_REVIEWED)
        held.merge_extra(
            set_keys={
                "shipping_hold": {
                    "reason": "awaiting review",
                    "skipped": "no shippable diff",
                    "held_at": timezone.now().isoformat(),
                    "checked_at": "",
                }
            }
        )
        cancelled = _aged_ticket(state=Ticket.State.PLAN_RECORDED)
        TaskFactory(ticket=cancelled, phase="coding", status=Task.Status.FAILED, failure_kind=FailureKind.CANCELLED)

        result = self._report([])
        listed = {item["ticket"] for item in result["stuck"]}
        sweep = {
            ticket.pk
            for ticket in idle_started_author_tickets(now=timezone.now(), threshold_hours=6)
            if not is_shipping_held(ticket)
        }

        assert listed == {tickets[state].pk for state in expected}
        assert listed == sweep
        assert recent.pk not in listed
        assert active.pk not in listed
        assert with_pr.pk not in listed
        assert held.pk not in listed
        assert cancelled.pk not in listed
        assert result["more_stuck"] == 0

    def test_unread_is_explicit_and_large_titles_stay_bounded(self) -> None:
        issues = [_issue(i, "fix: " + "x" * 2000) for i in range(1, 61)]
        tickets = [_aged_ticket(state=Ticket.State.WORK_STARTED, hours=48 + i) for i in range(60)]
        result = self._report(issues)
        assert len(json.dumps(result).encode()) <= 3072
        assert len(result["defects"]) == 5
        assert result["more_defects"] == 55
        assert [item["ticket"] for item in result["stuck"]] == [ticket.pk for ticket in reversed(tickets[-5:])]
        assert result["more_stuck"] == 55

        failed = self._report([], fail=True)
        assert failed["defects"] == []
        assert "forge unavailable" in " ".join(failed["unread"])

        with patch(_FACTORY, return_value=None):
            missing = run_landscape(Path("/does-not-exist"))["fix_first"]
        assert missing["defects"] == []
        assert missing["unread"]

        with (
            patch(_FACTORY, return_value=_Host([])),
            patch(_OVERLAY, return_value=_Overlay()),
            patch.object(_Overlay.review, "merge_candidate_repo_slugs", return_value=[]),
        ):
            no_repos = run_landscape(Path("/does-not-exist"))["fix_first"]
        assert no_repos["defects"] == []
        assert "no repository" in " ".join(no_repos["unread"])

    def test_long_references_reduce_list_count_to_hold_the_byte_cap(self) -> None:
        issues = [_issue(i, "fix: fault") for i in range(1, 6)]
        for index, issue in enumerate(issues, 1):
            issue["url"] = f"https://github.com/{'r' * 480}/{index}"
        for index in range(5):
            ticket = _aged_ticket(state=Ticket.State.WORK_STARTED, hours=48 + index)
            Ticket.objects.filter(pk=ticket.pk).update(issue_url=f"https://example.org/{'t' * 470}/{index}")

        result = self._report(issues)

        assert len(json.dumps(result).encode()) <= 3072
        assert result["more_defects"] + result["more_stuck"] > 0
        assert result["defects"]
        assert result["stuck"]
        assert all(item["issue"] in {issue["url"] for issue in issues} for item in result["defects"])

    def test_malformed_repo_read_is_explicit_and_never_lists_an_unlinked_issue(self) -> None:
        unlinked = self._report([{"title": "fix: missing link", "labels": ["bug"]}])
        assert unlinked["defects"] == []
        assert "missing issue URL" in " ".join(unlinked["unread"])

        with (
            patch(_FACTORY, return_value=_Host([])),
            patch(_OVERLAY, return_value=_Overlay()),
            patch.object(_Host, "list_repo_open_issues", return_value=None),
        ):
            invalid = run_landscape(Path("/does-not-exist"))["fix_first"]
        assert invalid["defects"] == []
        assert "invalid issue list" in " ".join(invalid["unread"])
