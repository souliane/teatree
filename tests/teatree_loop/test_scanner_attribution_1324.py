"""Scanner-side overlay attribution and bare-slug URL-gate fixes (#1324).

Two scanner-side bugs surface as wrong statusline rows:

* Namespaced MRs disappear from a given overlay's zone when the overlay's
    ``get_workspace_repos()`` returns bare names (``product``) instead of
    ``owner/product`` — the built URL prefix ``https://gitlab.com/product/``
    never matches the real namespaced URL
    ``https://gitlab.com/some-namespace/product/-/...``. Defect B:
    ``url_match_specificity`` must accept a wildcard owner segment so bare
    slugs gate correctly.
* A dogfooding overlay that lists a sibling overlay's repo under its own
    ``workspace_repos`` steals the sibling's PRs from its zone. Defect A:
    when a competing overlay claims the same URL more specifically,
    ``_url_allowed`` must drop the PR so the right zone keeps the row.
"""

from dataclasses import dataclass, field
from typing import Any

from teatree.core.backend_protocols import ReviewState
from teatree.loop.scanners.my_prs import MyPrsScanner
from teatree.loop.scanners.reviewer_prs import ReviewerPrsScanner
from teatree.loop.tick_resolvers import best_url_match_specificity, url_match_specificity, url_matches_prefix
from teatree.types import RawAPIDict


@dataclass
class _Host:
    user: str = "alice"
    prs: list[RawAPIDict] = field(default_factory=list)
    reviewer_prs: list[RawAPIDict] = field(default_factory=list)

    def current_user(self) -> str:
        return self.user

    def list_my_prs(self, *, author: str, updated_after: str | None = None) -> list[RawAPIDict]:
        _ = (author, updated_after)
        return list(self.prs)

    def list_review_requested_prs(
        self,
        *,
        reviewer: str,
        updated_after: str | None = None,
    ) -> list[RawAPIDict]:
        _ = (reviewer, updated_after)
        return list(self.reviewer_prs)

    def list_assigned_issues(self, *, assignee: str) -> list[RawAPIDict]:
        _ = assignee
        return []

    def get_review_state(self, *, pr_url: str, reviewer: str) -> ReviewState:
        _ = (pr_url, reviewer)
        return ReviewState.NONE

    def create_pr(self, spec: Any) -> RawAPIDict:
        _ = spec
        return {}

    def post_pr_comment(self, *, repo: str, pr_iid: int, body: str) -> RawAPIDict:
        _ = (repo, pr_iid, body)
        return {}

    def update_pr_comment(self, *, repo: str, pr_iid: int, comment_id: int, body: str) -> RawAPIDict:
        _ = (repo, pr_iid, comment_id, body)
        return {}

    def list_pr_comments(self, *, repo: str, pr_iid: int) -> list[RawAPIDict]:
        _ = (repo, pr_iid)
        return []

    def upload_file(self, *, repo: str, filepath: str) -> RawAPIDict:
        _ = (repo, filepath)
        return {}

    def get_issue(self, issue_url: str) -> RawAPIDict:
        _ = issue_url
        return {}

    def post_issue_comment(self, *, issue_url: str, body: str) -> RawAPIDict:
        _ = (issue_url, body)
        return {}


_NAMESPACED_MR = "https://gitlab.com/some-namespace/product/-/merge_requests/7487"
_OTHER_MR = "https://gitlab.com/other-namespace/elsewhere/-/merge_requests/1"


class TestUrlMatchSpecificity:
    """Specificity scoring lets the scanner break ties across overlay claims."""

    def test_plain_prefix_scores_full_length(self) -> None:
        prefix = "https://gitlab.com/some-namespace/product/"
        assert url_match_specificity(_NAMESPACED_MR, prefix) == len(prefix)

    def test_plain_prefix_zero_when_not_matching(self) -> None:
        assert url_match_specificity(_NAMESPACED_MR, "https://gitlab.com/elsewhere/x/") == 0

    def test_wildcard_matches_any_owner(self) -> None:
        prefix = "https://gitlab.com/*/product/"
        assert url_match_specificity(_NAMESPACED_MR, prefix) > 0

    def test_wildcard_zero_when_repo_differs(self) -> None:
        prefix = "https://gitlab.com/*/microservice-x/"
        assert url_match_specificity(_NAMESPACED_MR, prefix) == 0

    def test_plain_prefix_more_specific_than_wildcard(self) -> None:
        plain = "https://gitlab.com/some-namespace/product/"
        wildcard = "https://gitlab.com/*/product/"
        assert url_match_specificity(_NAMESPACED_MR, plain) > url_match_specificity(_NAMESPACED_MR, wildcard)

    def test_url_matches_prefix_backwards_compatible(self) -> None:
        assert url_matches_prefix(_NAMESPACED_MR, "https://gitlab.com/some-namespace/product/")
        assert url_matches_prefix(_NAMESPACED_MR, "https://gitlab.com/*/product/")
        assert not url_matches_prefix(_NAMESPACED_MR, "https://gitlab.com/other/x/")

    def test_best_specificity_picks_strongest_claim(self) -> None:
        prefixes = (
            "https://gitlab.com/elsewhere/x/",
            "https://gitlab.com/*/product/",
            "https://gitlab.com/some-namespace/product/",
        )
        best = best_url_match_specificity(_NAMESPACED_MR, prefixes)
        assert best == len("https://gitlab.com/some-namespace/product/")


class TestMyPrsBareSlugUrlGate:
    """Defect B: bare slug ``product`` must still admit namespaced MRs."""

    def test_namespaced_mr_admitted_through_wildcard_prefix(self) -> None:
        host = _Host(prs=[{"iid": 7487, "title": "x", "web_url": _NAMESPACED_MR}])
        # Mimics ``_allowed_url_prefixes_for_host`` output for a bare slug.
        scanner = MyPrsScanner(
            host=host,
            allowed_url_prefixes=("https://gitlab.com/*/product/",),
        )
        urls = sorted(s.payload["url"] for s in scanner.scan())
        assert urls == [_NAMESPACED_MR]

    def test_other_mr_rejected_by_wildcard_prefix(self) -> None:
        host = _Host(prs=[{"iid": 1, "title": "x", "web_url": _OTHER_MR}])
        scanner = MyPrsScanner(
            host=host,
            allowed_url_prefixes=("https://gitlab.com/*/product/",),
        )
        assert scanner.scan() == []


class TestMyPrsCompetingOverlayAttribution:
    """Defect A: the most-specific overlay claim wins attribution."""

    _SIBLING_PR = "https://github.com/sibling-owner/sibling-repo/pull/150"

    def test_pr_dropped_when_sibling_overlay_claims_more_specifically(self) -> None:
        host = _Host(prs=[{"iid": 150, "title": "sibling pr", "html_url": self._SIBLING_PR}])
        # The dogfooding overlay's workspace_repos lists the sibling's
        # repo via a wildcard slug, so its prefix matches; the sibling
        # overlay claims the same URL with the exact ``owner/repo``
        # prefix. Strictly more specific → the dogfooding scanner drops
        # the row so the sibling's zone keeps it.
        scanner_with_specific_sibling = MyPrsScanner(
            host=host,
            allowed_url_prefixes=("https://github.com/*/sibling-repo/",),
            competing_url_prefixes=("https://github.com/sibling-owner/sibling-repo/",),
        )
        assert scanner_with_specific_sibling.scan() == []

    def test_pr_kept_when_no_sibling_competes(self) -> None:
        host = _Host(prs=[{"iid": 150, "title": "sibling pr", "html_url": self._SIBLING_PR}])
        scanner = MyPrsScanner(
            host=host,
            allowed_url_prefixes=("https://github.com/sibling-owner/sibling-repo/",),
            competing_url_prefixes=(),
        )
        urls = sorted(s.payload["url"] for s in scanner.scan())
        assert urls == [self._SIBLING_PR]

    def test_pr_kept_when_sibling_claim_is_less_specific(self) -> None:
        host = _Host(prs=[{"iid": 150, "title": "sibling pr", "html_url": self._SIBLING_PR}])
        scanner = MyPrsScanner(
            host=host,
            allowed_url_prefixes=("https://github.com/sibling-owner/sibling-repo/",),
            competing_url_prefixes=("https://github.com/*/sibling-repo/",),
        )
        urls = sorted(s.payload["url"] for s in scanner.scan())
        assert urls == [self._SIBLING_PR]

    def test_unrelated_sibling_claim_is_ignored(self) -> None:
        host = _Host(prs=[{"iid": 150, "title": "sibling pr", "html_url": self._SIBLING_PR}])
        # Sibling claims a different repo entirely → does not compete.
        scanner = MyPrsScanner(
            host=host,
            allowed_url_prefixes=("https://github.com/*/sibling-repo/",),
            competing_url_prefixes=("https://github.com/other-owner/other-repo/",),
        )
        urls = sorted(s.payload["url"] for s in scanner.scan())
        assert urls == [self._SIBLING_PR]


class TestReviewerPrsAttributionMirrors:
    """Reviewer-prs uses the same gate as my-prs (`_url_allowed`)."""

    def test_namespaced_pr_admitted_through_wildcard_prefix(self, db: None) -> None:
        _ = db
        host = _Host(
            reviewer_prs=[{"web_url": _NAMESPACED_MR, "sha": "a"}],
        )
        scanner = ReviewerPrsScanner(
            host=host,
            allowed_url_prefixes=("https://gitlab.com/*/product/",),
        )
        urls = sorted(s.payload["url"] for s in scanner.scan())
        assert urls == [_NAMESPACED_MR]

    def test_pr_dropped_when_sibling_overlay_claims_more_specifically(self, db: None) -> None:
        _ = db
        pr_url = "https://github.com/sibling-owner/sibling-repo/pull/150"
        host = _Host(reviewer_prs=[{"web_url": pr_url, "sha": "a"}])
        scanner = ReviewerPrsScanner(
            host=host,
            allowed_url_prefixes=("https://github.com/*/sibling-repo/",),
            competing_url_prefixes=("https://github.com/sibling-owner/sibling-repo/",),
        )
        assert scanner.scan() == []
