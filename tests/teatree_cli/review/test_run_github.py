"""GitHub half of the ``t3 review run`` review-shape audit.

``souliane/teatree`` is itself a GitHub repo, so this is the path the review
skill's "canonical first command" takes on teatree's own PRs — the diff-size
classifier, the "no test files touched" heuristic, and the existing-review
summary all reach a teatree PR through here.

The payload-shape tests drive the CLI end to end against a stubbed
:class:`~teatree.backends.github.client.GitHubCodeHost` (no network) and pin the
result to the SAME shape the GitLab path emits, so a reviewer sub-agent parses
one contract regardless of forge. The aggregation helpers are pure, so they are
also exercised directly against raw GitHub payload shapes.
"""

import json
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from teatree.cli.review import review_app
from teatree.cli.review.run_github import (
    audit_github_pr,
    diff_stats_from_files,
    review_state_from_reviews,
    skip_verdict_for_open_state,
)
from teatree.core.backend_protocols import ApprovalState, PrOpenState
from teatree.utils.run import CommandFailedError

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

type JSONObject = dict[str, object]


GITHUB_PR_URL = "https://github.com/souliane/teatree/pull/6230"


class _StubGitHubCodeHost:
    """In-memory stub for the GitHub code host used by ``t3 review run``."""

    def __init__(
        self,
        *,
        files: list[JSONObject],
        reviews: list[JSONObject] | None = None,
        unresolved: int = 0,
        open_state: PrOpenState = PrOpenState.OPEN,
    ) -> None:
        self._files = files
        self._reviews = reviews or []
        self._unresolved = unresolved
        self._open_state = open_state

    def get_pr_diff(self, *, repo: str, pr_iid: int) -> list[JSONObject]:
        del repo, pr_iid
        return self._files

    def list_pr_reviews(self, *, repo: str, pr_iid: int) -> list[JSONObject]:
        del repo, pr_iid
        return self._reviews

    def get_mr_approvals(self, *, repo: str, pr_iid: int) -> ApprovalState:
        del repo, pr_iid
        return ApprovalState(approvals_left=0, approved_by=[], unresolved_resolvable=self._unresolved)

    def get_pr_open_state(self, *, pr_url: str) -> PrOpenState:
        del pr_url
        return self._open_state


def _file(*, path: str, added: int, removed: int) -> JSONObject:
    """One entry of GitHub's ``pulls/{n}/files`` payload."""
    return {"filename": path, "additions": added, "deletions": removed}


def _review(*, login: str, state: str) -> JSONObject:
    """One entry of GitHub's ``pulls/{n}/reviews`` payload."""
    return {"user": {"login": login}, "state": state}


class TestReviewRunGitHub:
    """A GitHub PR URL audits into the same payload the GitLab path emits (#1206).

    The keys, the complexity classifier, the findings catalog and the skip
    verdicts are shared, so a reviewer sub-agent parses one contract.
    """

    def test_emits_the_same_audit_shape_as_the_gitlab_path(self) -> None:
        stub = _StubGitHubCodeHost(
            files=[
                _file(path="src/foo.py", added=5, removed=2),
                _file(path="tests/test_foo.py", added=8, removed=0),
            ],
            reviews=[_review(login="reviewer-a", state="APPROVED"), _review(login="me", state="PENDING")],
            unresolved=1,
        )

        with patch("teatree.backends.github.client.GitHubCodeHost", return_value=stub):
            result = CliRunner().invoke(review_app, ["run", GITHUB_PR_URL])

        assert result.exit_code == 0, f"output={result.output!r} exc={result.exception!r}"
        payload = json.loads(result.output.strip())
        assert payload["mr"] == "souliane/teatree!6230"
        assert payload["forge"] == "github"
        assert payload["url"] == GITHUB_PR_URL
        assert payload["changes"] == {"files": 2, "additions": 13, "deletions": 2}
        assert payload["complexity"] == "trivial"
        assert payload["existing_review"]["open_discussions"] == 1
        assert payload["existing_review"]["draft_notes"] == 1
        assert payload["existing_review"]["approvals"] == 1
        assert payload["existing_review"]["approved_by"] == ["reviewer-a"]
        assert payload["verdict"] == "needs_attention"

    def test_large_github_pr_gets_the_split_finding_the_gitlab_path_emits(self) -> None:
        stub = _StubGitHubCodeHost(files=[_file(path="src/foo.py", added=600, removed=10)])

        with patch("teatree.backends.github.client.GitHubCodeHost", return_value=stub):
            result = CliRunner().invoke(review_app, ["run", GITHUB_PR_URL])

        assert result.exit_code == 0, f"output={result.output!r} exc={result.exception!r}"
        payload = json.loads(result.output.strip())
        assert payload["complexity"] == "large"
        assert any("large change" in finding for finding in payload["findings_catalog"]), payload
        assert any("no test files touched" in finding for finding in payload["findings_catalog"]), payload
        assert payload["verdict"] == "needs_attention"

    def test_merged_github_pr_short_circuits_to_skipped_merged(self) -> None:
        stub = _StubGitHubCodeHost(
            files=[_file(path="src/foo.py", added=5, removed=2)],
            open_state=PrOpenState.MERGED,
        )

        with patch("teatree.backends.github.client.GitHubCodeHost", return_value=stub):
            result = CliRunner().invoke(review_app, ["run", GITHUB_PR_URL])

        assert result.exit_code == 0, f"output={result.output!r} exc={result.exception!r}"
        assert json.loads(result.output.strip())["verdict"] == "skipped_merged"

    def test_unreadable_pr_state_fails_loud_rather_than_auditing_as_open(self) -> None:
        """An UNKNOWN open-state means the read failed — never a fabricated audit.

        ``get_pr_open_state`` maps every auth/network failure to ``UNKNOWN``
        (fail-open, so the orphan sweep never reaps on doubt). The audit consumes
        it with the opposite polarity: it cannot report ``ready_to_review`` for a
        PR whose live state it was unable to read.
        """
        stub = _StubGitHubCodeHost(
            files=[_file(path="src/foo.py", added=5, removed=2)],
            open_state=PrOpenState.UNKNOWN,
        )

        with patch("teatree.backends.github.client.GitHubCodeHost", return_value=stub):
            result = CliRunner().invoke(review_app, ["run", GITHUB_PR_URL])

        assert result.exit_code == 1, f"output={result.output!r} exc={result.exception!r}"
        payload = json.loads(result.output.strip())
        assert payload["error"] == "api_unavailable"
        assert "verdict" not in payload
        assert "changes" not in payload

    def test_backend_read_failure_surfaces_as_api_unavailable(self) -> None:
        class _RaisingHost:
            def get_pr_open_state(self, *, pr_url: str) -> PrOpenState:
                del pr_url
                return PrOpenState.OPEN

            def get_pr_diff(self, *, repo: str, pr_iid: int) -> list[JSONObject]:
                del repo, pr_iid
                raise CommandFailedError(["gh", "api"], returncode=1, stdout="", stderr="HTTP 403")

        with patch("teatree.backends.github.client.GitHubCodeHost", return_value=_RaisingHost()):
            result = CliRunner().invoke(review_app, ["run", GITHUB_PR_URL])

        assert result.exit_code == 1, f"output={result.output!r} exc={result.exception!r}"
        assert json.loads(result.output.strip())["error"] == "api_unavailable"


class TestDiffStatsFromFiles:
    """Counts come from GitHub's own per-file numbers, not from re-parsed hunks."""

    def test_sums_additions_deletions_and_collects_paths(self) -> None:
        stats = diff_stats_from_files(
            [
                _file(path="src/foo.py", added=5, removed=2),
                _file(path="tests/test_foo.py", added=8, removed=1),
            ]
        )

        assert stats.files == 2
        assert stats.additions == 13
        assert stats.deletions == 3
        assert stats.touched == ("src/foo.py", "tests/test_foo.py")

    def test_file_with_no_counts_still_counts_as_a_touched_file(self) -> None:
        """GitHub omits ``additions``/``deletions`` for some entries (binary, truncated)."""
        stats = diff_stats_from_files([{"filename": "logo.png"}])

        assert stats.files == 1
        assert stats.additions == 0
        assert stats.touched == ("logo.png",)


class TestReviewStateFromReviews:
    def test_counts_distinct_approvers_and_own_pending_reviews(self) -> None:
        state = review_state_from_reviews(
            [
                _review(login="reviewer-a", state="APPROVED"),
                _review(login="reviewer-b", state="COMMENTED"),
                _review(login="me", state="PENDING"),
            ],
            unresolved=2,
        )

        assert state.open_discussions == 2
        assert state.draft_notes == 1
        assert state.approvals == 1
        assert state.approved_by == ("reviewer-a",)

    def test_approval_superseded_by_later_changes_request_is_not_an_approval(self) -> None:
        state = review_state_from_reviews(
            [
                _review(login="reviewer-a", state="APPROVED"),
                _review(login="reviewer-a", state="CHANGES_REQUESTED"),
            ],
            unresolved=0,
        )

        assert state.approvals == 0
        assert state.approved_by == ()

    def test_no_reviews_reports_a_clean_but_unapproved_surface(self) -> None:
        state = review_state_from_reviews([], unresolved=0)

        assert state.open_discussions == 0
        assert state.draft_notes == 0
        assert state.approvals == 0
        assert state.approved_by == ()


class TestSkipVerdictForOpenState:
    def test_merged_and_closed_short_circuit_open_does_not(self) -> None:
        assert skip_verdict_for_open_state(PrOpenState.MERGED) == "skipped_merged"
        assert skip_verdict_for_open_state(PrOpenState.CLOSED) == "skipped_closed"
        assert skip_verdict_for_open_state(PrOpenState.OPEN) == ""


class TestAuditGitHubPrRejectsUnparsableUrl:
    def test_url_without_a_pr_number_raises_before_any_forge_read(self) -> None:
        with pytest.raises(ValueError, match="bad_url"):
            audit_github_pr("https://github.com/souliane/teatree")
