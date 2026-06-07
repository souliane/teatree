"""Behaviour tests for ``IssueDispositionScanner`` — DEAD-evidence triage (#2122).

The scanner lists ``needs-triage`` open issues and emits
``issue_disposition.close_candidate`` ONLY for the three machine-checkable dead
buckets. The conservative bar is load-bearing: ANY uncertainty yields no
candidate. Two anti-vacuity guards live here — a live in-flight ticket / unique
fingerprint / valid path yields ZERO candidates (revert the bar → RED), and the
default-OFF gate (exercised in ``test_issue_disposition_wiring``) emits nothing.
"""

from dataclasses import dataclass, field

from django.test import TestCase

from teatree.core.models import NEEDS_TRIAGE_LABEL, ImplementedIssueMarker, Ticket
from teatree.core.models.task import Task
from teatree.loop.scanners.issue_disposition import (
    CLOSE_CANDIDATE_KIND,
    IssueDispositionScanner,
    referenced_paths,
    title_fingerprint,
)
from teatree.types import RawAPIDict


@dataclass
class _Host:
    """Minimal CodeHostBackend stub — only the methods the scanner calls."""

    user: str = "alice"
    issues: list[RawAPIDict] = field(default_factory=list)
    open_issues_by_query: dict[str, list[RawAPIDict]] = field(default_factory=dict)

    def current_user(self) -> str:
        return self.user

    def list_assigned_issues(self, *, assignee: str) -> list[RawAPIDict]:
        _ = assignee
        return self.issues

    def search_open_issues(self, *, repo: str, query: str) -> list[RawAPIDict]:
        _ = repo
        return self.open_issues_by_query.get(query, [])


def _issue(url: str, *, title: str = "Do the thing", body: str = "", labels: list[str] | None = None) -> RawAPIDict:
    return {
        "web_url": url,
        "title": title,
        "body": body,
        "state": "open",
        "labels": labels if labels is not None else [NEEDS_TRIAGE_LABEL],
    }


class IssueDispositionAlreadyShippedTests(TestCase):
    REPO = "souliane/teatree"
    URL = "https://github.com/souliane/teatree/issues/300"

    def _scanner(self, host: _Host) -> IssueDispositionScanner:
        return IssueDispositionScanner(host=host, repo=self.REPO, overlay_name="acme")

    def test_delivered_ticket_for_issue_yields_already_shipped_candidate(self) -> None:
        Ticket.objects.create(issue_url=self.URL, state=Ticket.State.DELIVERED)
        signals = self._scanner(_Host(issues=[_issue(self.URL)])).scan()
        assert [s.kind for s in signals] == [CLOSE_CANDIDATE_KIND]
        assert signals[0].payload == {"url": self.URL, "reason": "already_shipped", "overlay": "acme"}

    def test_live_in_flight_ticket_yields_no_candidate(self) -> None:
        """Anti-vacuity (a): a live ticket on the URL FALSIFIES already-shipped."""
        Ticket.objects.create(issue_url=self.URL, state=Ticket.State.STARTED)
        assert self._scanner(_Host(issues=[_issue(self.URL)])).scan() == []

    def test_no_ticket_at_all_yields_no_candidate(self) -> None:
        assert self._scanner(_Host(issues=[_issue(self.URL)])).scan() == []


class IssueDispositionExactDuplicateTests(TestCase):
    REPO = "souliane/teatree"
    URL = "https://github.com/souliane/teatree/issues/400"
    OTHER = "https://github.com/souliane/teatree/issues/41"

    def _scanner(self, host: _Host) -> IssueDispositionScanner:
        return IssueDispositionScanner(host=host, repo=self.REPO, overlay_name="acme")

    def test_matching_open_issue_fingerprint_yields_duplicate_candidate(self) -> None:
        title = "Fix the broken login flow"
        host = _Host(
            issues=[_issue(self.URL, title=title)],
            open_issues_by_query={title: [_issue(self.OTHER, title="Fix the   BROKEN login flow")]},
        )
        signals = self._scanner(host).scan()
        assert [s.payload["reason"] for s in signals] == ["exact_duplicate"]

    def test_unique_fingerprint_yields_no_candidate(self) -> None:
        """Anti-vacuity (a): a unique fingerprint FALSIFIES exact-duplicate."""
        title = "A wholly unique issue title"
        host = _Host(
            issues=[_issue(self.URL, title=title)],
            open_issues_by_query={title: [_issue(self.OTHER, title="Something completely different")]},
        )
        assert self._scanner(host).scan() == []

    def test_self_match_is_not_a_duplicate(self) -> None:
        title = "Only one of these"
        host = _Host(
            issues=[_issue(self.URL, title=title)],
            open_issues_by_query={title: [_issue(self.URL, title=title)]},
        )
        assert self._scanner(host).scan() == []


class IssueDispositionObsoleteTests(TestCase):
    REPO = "souliane/teatree"
    URL = "https://github.com/souliane/teatree/issues/500"

    def test_all_referenced_paths_gone_yields_obsolete_candidate(self) -> None:
        host = _Host(issues=[_issue(self.URL, body="Broken in `src/teatree/gone.py` and `src/teatree/also_gone.py`.")])
        scanner = IssueDispositionScanner(
            host=host, repo=self.REPO, overlay_name="acme", path_exists=lambda _path: False
        )
        signals = scanner.scan()
        assert [s.payload["reason"] for s in signals] == ["obsolete"]

    def test_one_existing_path_yields_no_candidate(self) -> None:
        """Anti-vacuity (a): a single still-existing path FALSIFIES obsolete."""
        host = _Host(issues=[_issue(self.URL, body="See `src/teatree/here.py` and `src/teatree/gone.py`.")])
        scanner = IssueDispositionScanner(
            host=host,
            repo=self.REPO,
            overlay_name="acme",
            path_exists=lambda path: path == "src/teatree/here.py",
        )
        assert scanner.scan() == []

    def test_body_references_no_path_yields_no_candidate(self) -> None:
        host = _Host(issues=[_issue(self.URL, body="Just prose, run `git push`, no real file paths here.")])
        scanner = IssueDispositionScanner(
            host=host, repo=self.REPO, overlay_name="acme", path_exists=lambda _path: False
        )
        assert scanner.scan() == []

    def test_obsolete_bucket_disabled_without_oracle(self) -> None:
        host = _Host(issues=[_issue(self.URL, body="Broken in `src/teatree/gone.py`.")])
        assert IssueDispositionScanner(host=host, repo=self.REPO, overlay_name="acme").scan() == []


class IssueDispositionSelectionTests(TestCase):
    REPO = "souliane/teatree"
    URL = "https://github.com/souliane/teatree/issues/600"
    URL_B = "https://github.com/souliane/teatree/issues/601"

    def _scanner(self, host: _Host, **kw: object) -> IssueDispositionScanner:
        return IssueDispositionScanner(host=host, repo=self.REPO, overlay_name="acme", **kw)

    def test_issue_without_needs_triage_is_ignored(self) -> None:
        Ticket.objects.create(issue_url=self.URL, state=Ticket.State.DELIVERED)
        host = _Host(issues=[_issue(self.URL, labels=["bug"])])
        assert self._scanner(host).scan() == []

    def test_closed_needs_triage_issue_is_ignored(self) -> None:
        Ticket.objects.create(issue_url=self.URL, state=Ticket.State.DELIVERED)
        issue = _issue(self.URL)
        issue["state"] = "closed"
        assert self._scanner(_Host(issues=[issue])).scan() == []

    def test_no_identity_resolves_to_no_scan(self) -> None:
        Ticket.objects.create(issue_url=self.URL, state=Ticket.State.DELIVERED)
        assert self._scanner(_Host(user="", issues=[_issue(self.URL)])).scan() == []

    def test_max_closes_per_tick_bounds_emission(self) -> None:
        Ticket.objects.create(issue_url=self.URL, state=Ticket.State.DELIVERED)
        Ticket.objects.create(issue_url=self.URL_B, state=Ticket.State.DELIVERED)
        host = _Host(issues=[_issue(self.URL), _issue(self.URL_B)])
        assert len(self._scanner(host, max_closes_per_tick=1).scan()) == 1

    def test_scanner_creates_no_tasks_or_markers(self) -> None:
        Ticket.objects.create(issue_url=self.URL, state=Ticket.State.DELIVERED)
        self._scanner(_Host(issues=[_issue(self.URL)])).scan()
        assert Task.objects.count() == 0
        assert ImplementedIssueMarker.objects.count() == 0


class IssueDispositionPureHelperTests(TestCase):
    def test_title_fingerprint_normalizes_whitespace_and_case(self) -> None:
        assert title_fingerprint("  Fix   the BUG ") == title_fingerprint("fix the bug")

    def test_referenced_paths_keeps_only_pathlike_tokens(self) -> None:
        body = "Touch `src/a/b.py`, not `git status`, also `docs/x.md`."
        assert referenced_paths(body) == ["src/a/b.py", "docs/x.md"]
