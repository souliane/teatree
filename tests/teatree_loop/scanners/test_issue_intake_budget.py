"""Intake spends its budget on the frontier, and resumes there when it runs out (#4466).

The scanner walks candidates oldest-filed first under a 60s scan deadline it owns alone. The
walk cost the PRODUCT of the candidate set and the PR corpus, so the budget was exhausted
before the newest issues were reached — and every abandoned pass restarted from the oldest,
so the same tail was dropped forever. Newly-filed issues were therefore never admitted.

The clock is injected and advances one second per candidate examined, so "the budget ran
out" is a fixed candidate index rather than a wall-clock race.
"""

import itertools
from collections.abc import Callable
from dataclasses import dataclass, field
from unittest.mock import patch

from django.test import TestCase

from teatree.core.models import IntakeScanCursor, Ticket, UnclaimedIntakeCandidate, WaitingCandidate
from teatree.loop.scanners.issue_intake import IssueIntakeScanner
from teatree.types import RawAPIDict

OWNER = "souliane"
OVERLAY = "acme"
LABEL = "auto-implement"
REPO = "https://github.com/souliane/teatree"


def _url(number: int) -> str:
    return f"{REPO}/issues/{number}"


def _issue(number: int, *, created_at: str) -> RawAPIDict:
    return {
        "web_url": _url(number),
        "title": f"issue {number}",
        "labels": [],
        "state": "open",
        "user": {"login": OWNER},
        "body": "",
        "created_at": created_at,
    }


def _pr(number: int) -> RawAPIDict:
    return {"html_url": f"{REPO}/pull/{number}", "head": {"ref": f"{number}-branch"}, "body": "", "title": ""}


@dataclass
class _Host:
    authored: dict[str, list[RawAPIDict]] = field(default_factory=dict)
    open_prs: list[RawAPIDict] = field(default_factory=list)
    merged_prs: list[RawAPIDict] = field(default_factory=list)
    enrich_requested: list[bool] = field(default_factory=list)

    def current_user(self) -> str:
        return OWNER

    def list_authored_issues(self, *, author: str, repo_slugs: tuple[str, ...] = ()) -> list[RawAPIDict]:
        _ = repo_slugs
        return list(self.authored.get(author, []))

    def list_labeled_issues(self, *, label: str, repo_slugs: tuple[str, ...] = ()) -> list[RawAPIDict]:
        _ = (label, repo_slugs)
        return []

    def list_my_prs(self, *, author: str, enrich: bool = True) -> list[RawAPIDict]:
        _ = author
        self.enrich_requested.append(enrich)
        return self.open_prs

    def list_my_merged_prs(self, *, author: str) -> list[RawAPIDict]:
        _ = author
        return self.merged_prs


def _one_second_per_candidate() -> Callable[[], float]:
    """A monotonic clock advancing 1s per read — one candidate's examination per second."""
    ticks = itertools.count(0.0, 1.0)
    return lambda: next(ticks)


class _IntakeTestCase(TestCase):
    def setUp(self) -> None:
        patcher = patch("teatree.core.review.author_trust.repo_is_internal", return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        # The governor reads live box load, so on a busy host it denies admission and the
        # scan claims nothing — which is not what any test below is about.
        governor = patch("teatree.core.agent_admission.agent_admission_denied_reason", return_value=None)
        governor.start()
        self.addCleanup(governor.stop)

    def _scanner(self, host: _Host, **overrides: object) -> IssueIntakeScanner:
        kwargs: dict[str, object] = {
            "host": host,
            "admit_label": LABEL,
            "overlay_name": OVERLAY,
            "trusted_authors": (OWNER,),
            "identities": (OWNER,),
        }
        kwargs.update(overrides)
        return IssueIntakeScanner(**kwargs)


class FrontierIsReachedTests(_IntakeTestCase):
    """The acceptance criterion: N already-decided issues plus one fresh one, in ONE pass."""

    def test_the_fresh_candidate_is_admitted_behind_199_decided_ones(self) -> None:
        decided = [_issue(n, created_at=f"2026-01-01T00:{n % 60:02d}:00Z") for n in range(1, 200)]
        fresh = _issue(500, created_at="2026-08-14T21:45:00Z")
        for issue in decided:
            Ticket.objects.create(issue_url=str(issue["web_url"]), overlay=OVERLAY, state=Ticket.State.STARTED)
        host = _Host(authored={OWNER: [*decided, fresh]})

        signals = self._scanner(host).scan()

        assert [signal.payload["url"] for signal in signals] == [_url(500)]

    def test_the_pass_records_itself_complete(self) -> None:
        host = _Host(authored={OWNER: [_issue(1, created_at="2026-01-01T00:00:00Z")]})

        self._scanner(host).scan()

        cursor = IntakeScanCursor.objects.get(overlay=OVERLAY)
        assert cursor.consecutive_incomplete_passes == 0


class ReadbackCostTests(_IntakeTestCase):
    """``ignore_work_exists`` must not cost a scan of the whole PR corpus per candidate."""

    def test_the_open_pr_fetch_does_not_ask_for_the_per_pr_ci_enrichment(self) -> None:
        host = _Host(authored={OWNER: [_issue(1, created_at="2026-01-01T00:00:00Z")]})

        self._scanner(host).scan()

        assert host.enrich_requested == [False]

    def test_a_merged_pr_citing_the_issue_still_holds_it(self) -> None:
        host = _Host(
            authored={OWNER: [_issue(7, created_at="2026-01-01T00:00:00Z")]},
            merged_prs=[_pr(7)],
        )

        assert self._scanner(host).scan() == []


class PartialPassTests(_IntakeTestCase):
    """A pass that runs out of budget resumes at the frontier rather than restarting."""

    CANDIDATES = 10
    BUDGET = 4.0

    def _issues(self) -> list[RawAPIDict]:
        return [_issue(n, created_at=f"2026-01-0{n}T00:00:00Z") for n in range(1, self.CANDIDATES + 1)]

    def _partial_scanner(self, host: _Host) -> IssueIntakeScanner:
        return self._scanner(
            host,
            can_claim=False,
            pass_budget_seconds=self.BUDGET,
            monotonic=_one_second_per_candidate(),
        )

    def test_the_first_pass_stops_at_the_budget(self) -> None:
        host = _Host(authored={OWNER: self._issues()})

        self._partial_scanner(host).scan()

        waiting = set(UnclaimedIntakeCandidate.objects.filter(overlay=OVERLAY).values_list("issue_url", flat=True))
        assert waiting == {_url(n) for n in range(1, 4)}

    def test_the_next_pass_resumes_after_the_cursor(self) -> None:
        host = _Host(authored={OWNER: self._issues()})
        self._partial_scanner(host).scan()

        self._partial_scanner(host).scan()

        cursor = IntakeScanCursor.objects.get(overlay=OVERLAY)
        assert cursor.last_issue_url == _url(6)

    def test_a_partial_pass_keeps_the_waiting_rows_it_did_not_examine(self) -> None:
        UnclaimedIntakeCandidate.objects.sync(
            OVERLAY,
            [WaitingCandidate(issue_url=_url(9), title="issue 9")],
        )
        host = _Host(authored={OWNER: self._issues()})

        self._partial_scanner(host).scan()

        assert UnclaimedIntakeCandidate.objects.filter(overlay=OVERLAY, issue_url=_url(9)).exists()

    def test_a_complete_pass_still_evicts_a_stale_waiting_row(self) -> None:
        UnclaimedIntakeCandidate.objects.sync(
            OVERLAY,
            [WaitingCandidate(issue_url=_url(999), title="gone")],
        )
        host = _Host(authored={OWNER: self._issues()})

        self._scanner(host, can_claim=False).scan()

        assert not UnclaimedIntakeCandidate.objects.filter(overlay=OVERLAY, issue_url=_url(999)).exists()

    def test_the_walk_wraps_to_the_oldest_once_past_the_frontier(self) -> None:
        host = _Host(authored={OWNER: self._issues()})
        for _ in range(3):
            self._partial_scanner(host).scan()

        self._partial_scanner(host).scan()

        cursor = IntakeScanCursor.objects.get(overlay=OVERLAY)
        assert cursor.last_issue_url == _url(2)

    def test_repeated_partial_passes_are_counted(self) -> None:
        host = _Host(authored={OWNER: self._issues()})

        self._partial_scanner(host).scan()
        self._partial_scanner(host).scan()

        cursor = IntakeScanCursor.objects.get(overlay=OVERLAY)
        assert cursor.consecutive_incomplete_passes == 2

    def test_a_complete_pass_clears_the_incomplete_streak(self) -> None:
        host = _Host(authored={OWNER: self._issues()})
        self._partial_scanner(host).scan()

        self._scanner(host, can_claim=False).scan()

        cursor = IntakeScanCursor.objects.get(overlay=OVERLAY)
        assert cursor.consecutive_incomplete_passes == 0


class AgeOrderFairnessTests(_IntakeTestCase):
    """Resuming must not rotate the queue for a pass that FINISHED (#4238).

    The oldest admissible issue takes the first slot that frees. A resume point carried
    into a completed pass starts the next walk at a newer issue, so the slot goes to it
    and the longest-waiting issue keeps losing — the exact starvation #4238 closed.
    """

    OLD = _url(4188)

    def _queue(self) -> list[RawAPIDict]:
        return [
            _issue(4188, created_at="2026-08-01T09:00:00Z"),
            _issue(4230, created_at="2026-08-02T09:00:00Z"),
            _issue(4330, created_at="2026-08-03T09:00:00Z"),
        ]

    def test_a_completed_pass_leaves_no_resume_point(self) -> None:
        host = _Host(authored={OWNER: self._queue()})
        self._scanner(host, can_claim=False).scan()

        assert IntakeScanCursor.objects.resume_after(OVERLAY) == ""

    def test_the_oldest_issue_takes_the_slot_after_a_completed_pass(self) -> None:
        host = _Host(authored={OWNER: self._queue()})
        self._scanner(host, can_claim=False).scan()

        signals = self._scanner(host, max_concurrent=1).scan()

        assert [signal.payload["url"] for signal in signals] == [self.OLD]

    def test_an_incomplete_pass_does_leave_a_resume_point(self) -> None:
        host = _Host(authored={OWNER: self._queue()})

        # 3s examines two candidates (the deadline is read once before the walk), so the
        # resume point is genuinely past the head of the queue.
        self._scanner(
            host,
            can_claim=False,
            pass_budget_seconds=3.0,
            monotonic=_one_second_per_candidate(),
        ).scan()

        assert IntakeScanCursor.objects.resume_after(OVERLAY) == _url(4230)
