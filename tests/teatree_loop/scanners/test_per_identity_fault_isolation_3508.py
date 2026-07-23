"""Per-identity fault isolation across the identity-fan-out scanners (#3508).

Five scanners fan out one forge query per identity (author / reviewer /
assignee) with no ``try`` around the per-identity call, so one failing
identity — a rate limit, a deleted account, a transient forge error —
aborted the whole scanner pass and dropped every OTHER identity's signals
for that tick. The corrected sibling is
``forge_readback._union_prs``: fetch wrapped in ``try/except`` +
``logger.warning`` + ``continue``.

Each test makes the FIRST queried identity's fetch raise and asserts the
SECOND identity's signal still surfaces (RED on the unfixed code — the
whole scan raised).
"""

from dataclasses import dataclass, field
from unittest.mock import patch

from django.test import TestCase

from teatree.core.backend_protocols import ReviewState
from teatree.core.models import NEEDS_TRIAGE_LABEL, ImplementedIssueMarker
from teatree.loop.scanners.issue_intake import IssueIntakeScanner
from teatree.loop.scanners.my_prs import MyPrsScanner
from teatree.loop.scanners.needs_triage_query import needs_triage_issues
from teatree.loop.scanners.reviewer_prs import ReviewerPrsScanner
from teatree.types import RawAPIDict

_BAD = "bad-identity"
_BAD_LABEL = "bad-identity"
_GOOD = "good-identity"


@dataclass
class _RaisingIdentityHost:
    """Forge host that RAISES for one identity's fetch and returns for another.

    Models the #3508 failure: the per-identity forge call itself throws (rate
    limit, deleted account, transient error), not the downstream item
    processing.
    """

    user: str = _GOOD
    prs_by_author: dict[str, list[RawAPIDict]] = field(default_factory=dict)
    review_requested_by_reviewer: dict[str, list[RawAPIDict]] = field(default_factory=dict)
    issues_by_assignee: dict[str, list[RawAPIDict]] = field(default_factory=dict)
    authored_issues_by_author: dict[str, list[RawAPIDict]] = field(default_factory=dict)
    issues_by_label: dict[str, list[RawAPIDict]] = field(default_factory=dict)
    raise_for_identity: str = _BAD

    def current_user(self) -> str:
        return self.user

    def _guard(self, identity: str) -> None:
        if identity == self.raise_for_identity:
            msg = f"simulated forge failure for {identity}"
            raise RuntimeError(msg)

    def list_my_prs(self, *, author: str, updated_after: str | None = None) -> list[RawAPIDict]:
        _ = updated_after
        self._guard(author)
        return list(self.prs_by_author.get(author, ()))

    def list_review_requested_prs(self, *, reviewer: str, updated_after: str | None = None) -> list[RawAPIDict]:
        _ = updated_after
        self._guard(reviewer)
        return list(self.review_requested_by_reviewer.get(reviewer, ()))

    def list_assigned_issues(self, *, assignee: str) -> list[RawAPIDict]:
        self._guard(assignee)
        return list(self.issues_by_assignee.get(assignee, ()))

    def list_authored_issues(self, *, author: str, repo_slugs: tuple[str, ...] = ()) -> list[RawAPIDict]:
        _ = repo_slugs
        self._guard(author)
        return list(self.authored_issues_by_author.get(author, ()))

    def list_labeled_issues(self, *, label: str, repo_slugs: tuple[str, ...] = ()) -> list[RawAPIDict]:
        _ = repo_slugs
        self._guard(label)
        return list(self.issues_by_label.get(label, ()))

    def list_my_merged_prs(self, *, author: str, updated_after: str | None = None) -> list[RawAPIDict]:
        _ = (author, updated_after)
        return []

    def get_review_state(self, *, pr_url: str, reviewer: str) -> ReviewState:
        _ = (pr_url, reviewer)
        return ReviewState.NONE

    def get_issue(self, issue_url: str) -> RawAPIDict:
        _ = issue_url
        return {}


class TestMyPrsPerIdentityIsolation(TestCase):
    def test_failing_identity_does_not_suppress_sibling_prs(self) -> None:
        host = _RaisingIdentityHost(
            prs_by_author={_GOOD: [{"iid": 1, "title": "A", "web_url": "https://x/1"}]},
        )
        scanner = MyPrsScanner(host=host, identities=(_BAD, _GOOD))
        signals = scanner.scan()
        assert [s.payload["url"] for s in signals] == ["https://x/1"]


class TestReviewerPrsPerIdentityIsolation(TestCase):
    def test_failing_identity_does_not_suppress_sibling_prs(self) -> None:
        host = _RaisingIdentityHost(
            review_requested_by_reviewer={_GOOD: [{"web_url": "https://gl/r/2", "sha": "b"}]},
        )
        scanner = ReviewerPrsScanner(host=host, identities=(_BAD, _GOOD))
        signals = scanner.scan()
        urls = [s.payload["url"] for s in signals]
        assert "https://gl/r/2" in urls


class TestNeedsTriageQueryPerIdentityIsolation(TestCase):
    def test_failing_assignee_does_not_suppress_sibling_issues(self) -> None:
        good_issue: RawAPIDict = {
            "web_url": "https://gl/i/9",
            "state": "open",
            "labels": [NEEDS_TRIAGE_LABEL],
        }
        host = _RaisingIdentityHost(issues_by_assignee={_GOOD: [good_issue]})
        issues = needs_triage_issues(host, (_BAD, _GOOD))
        assert [i["web_url"] for i in issues] == ["https://gl/i/9"]


class TestIssueIntakePerIdentityIsolation(TestCase):
    def setUp(self) -> None:
        patcher = patch("teatree.core.review.author_trust.repo_is_internal", return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _issue(self, url: str, author: str) -> RawAPIDict:
        return {
            "web_url": url,
            "title": "do it",
            "labels": ["auto-implement"],
            "state": "open",
            "user": {"login": author},
        }

    def test_failing_author_does_not_suppress_sibling_issue(self) -> None:
        good_url = "https://github.com/acme/repo/issues/2"
        host = _RaisingIdentityHost(
            authored_issues_by_author={_GOOD: [self._issue(good_url, _GOOD)]},
        )
        scanner = IssueIntakeScanner(
            host=host,
            admit_label="auto-implement",
            overlay_name="acme",
            trusted_authors=(_BAD, _GOOD),
        )
        signals = scanner.scan()
        assert [s.payload["url"] for s in signals] == [good_url]
        assert ImplementedIssueMarker.objects.filter(issue_url=good_url).exists()

    def test_failing_admit_label_query_does_not_suppress_authored_issues(self) -> None:
        """The #3634 label query is the surface #3508 never saw — it needs the same guard."""
        good_url = "https://github.com/acme/repo/issues/3"
        host = _RaisingIdentityHost(
            authored_issues_by_author={_GOOD: [self._issue(good_url, _GOOD)]},
            raise_for_identity=_BAD_LABEL,
        )
        scanner = IssueIntakeScanner(
            host=host,
            admit_label=_BAD_LABEL,
            overlay_name="acme",
            trusted_authors=(_GOOD,),
        )

        signals = scanner.scan()

        assert [s.payload["url"] for s in signals] == [good_url]

    def test_failing_author_query_does_not_suppress_the_label_admitted_issue(self) -> None:
        """The reverse direction: a stranger's admitted issue survives a bad author fan-out."""
        admitted_url = "https://github.com/acme/repo/issues/4"
        host = _RaisingIdentityHost(
            issues_by_label={"auto-implement": [self._issue(admitted_url, "random-stranger")]},
        )
        scanner = IssueIntakeScanner(
            host=host,
            admit_label="auto-implement",
            overlay_name="acme",
            trusted_authors=(_BAD, _GOOD),
        )

        signals = scanner.scan()

        assert [s.payload["url"] for s in signals] == [admitted_url]
