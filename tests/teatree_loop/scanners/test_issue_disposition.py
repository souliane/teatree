"""Behaviour tests for ``IssueDispositionScanner`` — DEAD-evidence triage (#2122).

The scanner lists ``needs-triage`` open issues and emits
``issue_disposition.close_candidate`` ONLY for the three machine-checkable dead
buckets. The conservative bar is load-bearing: ANY uncertainty yields no
candidate. Two anti-vacuity guards live here — a live in-flight ticket / unique
fingerprint / valid path yields ZERO candidates (revert the bar → RED), and the
canonical-core overlay guard (exercised in ``test_issue_disposition_wiring``)
keeps other backlogs out.
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
    open_issues_by_repo_and_query: dict[tuple[str, str], list[RawAPIDict]] = field(default_factory=dict)
    searched_repos: list[str] = field(default_factory=list)

    def current_user(self) -> str:
        return self.user

    def list_assigned_issues(self, *, assignee: str) -> list[RawAPIDict]:
        _ = assignee
        return self.issues

    def search_open_issues(self, *, repo: str, query: str) -> list[RawAPIDict]:
        self.searched_repos.append(repo)
        return self.open_issues_by_repo_and_query.get((repo, query), self.open_issues_by_query.get(query, []))

    def repo_for_issue_url(self, issue_url: str) -> str:
        return _slug_of(issue_url)


def _slug_of(issue_url: str) -> str:
    """``owner/name`` from a forge issue URL, the way a real code host answers it."""
    parts = issue_url.split("/")
    return "/".join(parts[3:5]) if len(parts) > 5 else ""


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
        return IssueDispositionScanner(host=host, overlay_name="acme")

    def test_delivered_ticket_for_issue_yields_already_shipped_candidate(self) -> None:
        Ticket.objects.create(issue_url=self.URL, state=Ticket.State.DELIVERED)
        signals = self._scanner(_Host(issues=[_issue(self.URL)])).scan()
        assert [s.kind for s in signals] == [CLOSE_CANDIDATE_KIND]
        assert signals[0].payload == {"url": self.URL, "reason": "already_shipped", "overlay": "acme"}

    def test_live_in_flight_ticket_yields_no_candidate(self) -> None:
        """Anti-vacuity (a): a live ticket on the URL FALSIFIES already-shipped."""
        Ticket.objects.create(issue_url=self.URL, state=Ticket.State.WORK_STARTED)
        assert self._scanner(_Host(issues=[_issue(self.URL)])).scan() == []

    def test_no_ticket_at_all_yields_no_candidate(self) -> None:
        assert self._scanner(_Host(issues=[_issue(self.URL)])).scan() == []


class IssueDispositionExactDuplicateTests(TestCase):
    REPO = "souliane/teatree"
    URL = "https://github.com/souliane/teatree/issues/400"
    OTHER = "https://github.com/souliane/teatree/issues/41"

    def _scanner(self, host: _Host) -> IssueDispositionScanner:
        return IssueDispositionScanner(host=host, overlay_name="acme")

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

    def test_of_two_duplicates_only_the_newer_is_a_candidate_and_it_names_the_survivor(self) -> None:
        title = "Fix the broken login flow"
        older, newer = _issue(self.OTHER, title=title), _issue(self.URL, title=title)
        host = _Host(issues=[older, newer], open_issues_by_query={title: [older, newer]})

        signals = self._scanner(host).scan()

        assert [(s.payload["url"], s.payload["duplicate_of"]) for s in signals] == [(self.URL, self.OTHER)]

    def test_a_sibling_with_no_issue_number_leaves_the_survivor_undecided(self) -> None:
        title = "Fix the broken login flow"
        unnumbered = _issue("https://github.com/souliane/teatree/issues/new", title=title)
        host = _Host(issues=[_issue(self.URL, title=title)], open_issues_by_query={title: [unnumbered]})

        assert self._scanner(host).scan() == []

    def test_self_match_is_not_a_duplicate(self) -> None:
        title = "Only one of these"
        host = _Host(
            issues=[_issue(self.URL, title=title)],
            open_issues_by_query={title: [_issue(self.URL, title=title)]},
        )
        assert self._scanner(host).scan() == []


class IssueDispositionIsScopeBoundToTheCandidatesOwnRepoTests(TestCase):
    """The listing spans every owned repo, so the duplicate search must follow the candidate.

    Searching one fixed repo while listing across all of them compares issues that share
    a title but nothing else, and orders the group by a bare issue number that is not
    comparable across repos — so a second repo's issue is closed in favour of a
    first-repo issue it has no relationship to.
    """

    TITLE = "Fix the broken login flow"
    A_URL = "https://github.com/souliane/repo-a/issues/10"
    B_URL = "https://github.com/souliane/repo-b/issues/20"

    def _scanner(self, host: _Host) -> IssueDispositionScanner:
        return IssueDispositionScanner(host=host, overlay_name="acme")

    def test_a_same_title_issue_in_another_repo_is_not_a_duplicate(self) -> None:
        a, b = _issue(self.A_URL, title=self.TITLE), _issue(self.B_URL, title=self.TITLE)
        host = _Host(
            issues=[a, b],
            open_issues_by_repo_and_query={
                ("souliane/repo-a", self.TITLE): [a],
                ("souliane/repo-b", self.TITLE): [b],
            },
        )

        assert self._scanner(host).scan() == []
        assert set(host.searched_repos) == {"souliane/repo-a", "souliane/repo-b"}

    def test_two_duplicates_in_one_repo_still_close_the_higher_numbered(self) -> None:
        older = _issue("https://github.com/souliane/repo-a/issues/10", title=self.TITLE)
        newer = _issue("https://github.com/souliane/repo-a/issues/400", title=self.TITLE)
        host = _Host(
            issues=[older, newer],
            open_issues_by_repo_and_query={("souliane/repo-a", self.TITLE): [older, newer]},
        )

        signals = self._scanner(host).scan()

        assert [(s.payload["url"], s.payload["duplicate_of"]) for s in signals] == [
            ("https://github.com/souliane/repo-a/issues/400", "https://github.com/souliane/repo-a/issues/10")
        ]

    def test_an_unresolvable_repo_leaves_the_duplicate_bucket_silent(self) -> None:
        host = _Host(issues=[_issue("not-a-url", title=self.TITLE)])

        assert self._scanner(host).scan() == []
        assert host.searched_repos == []


class IssueDispositionObsoleteTests(TestCase):
    REPO = "souliane/teatree"
    URL = "https://github.com/souliane/teatree/issues/500"

    def test_all_referenced_paths_gone_yields_obsolete_candidate(self) -> None:
        host = _Host(issues=[_issue(self.URL, body="Broken in `src/teatree/gone.py` and `src/teatree/also_gone.py`.")])
        scanner = IssueDispositionScanner(host=host, overlay_name="acme", path_exists=lambda _repo, _path: False)
        signals = scanner.scan()
        assert [s.payload["reason"] for s in signals] == ["obsolete"]

    def test_one_existing_path_yields_no_candidate(self) -> None:
        """Anti-vacuity (a): a single still-existing path FALSIFIES obsolete."""
        host = _Host(issues=[_issue(self.URL, body="See `src/teatree/here.py` and `src/teatree/gone.py`.")])
        scanner = IssueDispositionScanner(
            host=host,
            overlay_name="acme",
            path_exists=lambda _repo, path: path == "src/teatree/here.py",
        )
        assert scanner.scan() == []

    def test_body_references_no_path_yields_no_candidate(self) -> None:
        host = _Host(issues=[_issue(self.URL, body="Just prose, run `git push`, no real file paths here.")])
        scanner = IssueDispositionScanner(host=host, overlay_name="acme", path_exists=lambda _repo, _path: False)
        assert scanner.scan() == []

    def test_obsolete_bucket_disabled_without_oracle(self) -> None:
        host = _Host(issues=[_issue(self.URL, body="Broken in `src/teatree/gone.py`.")])
        assert IssueDispositionScanner(host=host, overlay_name="acme").scan() == []


class IssueDispositionSelectionTests(TestCase):
    REPO = "souliane/teatree"
    URL = "https://github.com/souliane/teatree/issues/600"
    URL_B = "https://github.com/souliane/teatree/issues/601"

    def _scanner(self, host: _Host, **kw: object) -> IssueDispositionScanner:
        return IssueDispositionScanner(host=host, overlay_name="acme", **kw)

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
