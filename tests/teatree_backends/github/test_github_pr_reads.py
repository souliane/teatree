"""Behaviour of the GitHub PR read helpers (status rollup, review threads, approvals)."""

import json
import subprocess
from unittest.mock import patch

from teatree.backends.github import pr_reads
from teatree.core.backend_protocols import PrOpenState
from teatree.utils.run import CommandFailedError


def _completed(stdout: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 0, stdout, "")


class TestRollupState:
    def test_empty_or_non_list_is_blank(self) -> None:
        assert pr_reads.rollup_state([]) == ""
        assert pr_reads.rollup_state(None) == ""

    def test_all_success(self) -> None:
        rollup = [{"status": "COMPLETED", "conclusion": "SUCCESS"}, {"state": "SUCCESS"}]
        assert pr_reads.rollup_state(rollup) == "success"

    def test_failure_dominates(self) -> None:
        rollup = [{"status": "COMPLETED", "conclusion": "SUCCESS"}, {"status": "COMPLETED", "conclusion": "FAILURE"}]
        assert pr_reads.rollup_state(rollup) == "failure"

    def test_in_progress_is_pending(self) -> None:
        rollup = [{"status": "IN_PROGRESS", "conclusion": None}, {"state": "SUCCESS"}]
        assert pr_reads.rollup_state(rollup) == "pending"


class TestIsNotFound:
    def test_true_on_http_404(self) -> None:
        assert pr_reads.is_not_found(CommandFailedError(["gh"], 1, "", "HTTP 404")) is True

    def test_false_on_other_error(self) -> None:
        assert pr_reads.is_not_found(CommandFailedError(["gh"], 1, "", "HTTP 500")) is False


class TestIssueRepoShort:
    def test_parses_issue_url(self) -> None:
        assert pr_reads.issue_repo_short("https://github.com/souliane/teatree/issues/50") == "teatree"

    def test_parses_pr_url(self) -> None:
        assert pr_reads.issue_repo_short("https://github.com/org/widget/pull/7") == "widget"

    def test_blank_for_unparseable(self) -> None:
        assert pr_reads.issue_repo_short("https://example.com/not/an/issue") == ""


class TestEnrichPrPipeline:
    def test_enriches_hit_with_head_sha_and_rollup(self) -> None:
        detail = json.dumps(
            {"headRefOid": "cafef00d", "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "FAILURE"}]}
        )
        hit = {"number": 9, "html_url": "https://github.com/o/r/pull/9"}
        with patch.object(pr_reads, "_run_gh", return_value=_completed(detail)):
            enriched = pr_reads.enrich_pr_pipeline(hit, token="tok")
        assert enriched["sha"] == "cafef00d"
        assert enriched["status_check_rollup"] == {"state": "failure"}

    def test_unparseable_url_left_unenriched(self) -> None:
        with patch.object(pr_reads, "_run_gh") as mock_run:
            enriched = pr_reads.enrich_pr_pipeline({"number": 1, "html_url": "not-a-url"}, token="tok")
        mock_run.assert_not_called()
        assert "status_check_rollup" not in enriched

    def test_read_failure_leaves_hit_unenriched(self) -> None:
        with patch.object(pr_reads, "_run_gh", side_effect=CommandFailedError(["gh"], 1, "", "boom")):
            enriched = pr_reads.enrich_pr_pipeline({"html_url": "https://github.com/o/r/pull/9"}, token="tok")
        assert "sha" not in enriched


class TestCountUnresolvedReviewThreads:
    def _threads(self, *, unresolved: int) -> str:
        nodes = [{"isResolved": False}] * unresolved
        return json.dumps({"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": nodes}}}}})

    def test_counts_unresolved(self) -> None:
        with patch.object(pr_reads, "_run_gh", return_value=_completed(self._threads(unresolved=2))):
            assert pr_reads.count_unresolved_review_threads(repo="o/r", pr_iid=9, token="t") == 2

    def test_malformed_slug_is_none(self) -> None:
        assert pr_reads.count_unresolved_review_threads(repo="no-slash", pr_iid=9, token="t") is None

    def test_read_failure_is_none(self) -> None:
        with patch.object(pr_reads, "_run_gh", side_effect=CommandFailedError(["gh"], 1, "", "boom")):
            assert pr_reads.count_unresolved_review_threads(repo="o/r", pr_iid=9, token="t") is None


class TestApprovalState:
    def _route(self, *, decision: str, threads: str):
        def _run(*args: str, **_: object) -> subprocess.CompletedProcess[str]:
            return _completed(threads if "graphql" in args else decision)

        return patch.object(pr_reads, "_run_gh", side_effect=_run)

    def test_approved_is_zero_approvals_left(self) -> None:
        threads = json.dumps({"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": []}}}}})
        with self._route(decision=json.dumps({"reviewDecision": "APPROVED"}), threads=threads):
            state = pr_reads.approval_state(repo="o/r", pr_iid=9, token="t")
        assert state["approvals_left"] == 0
        assert state["unresolved_resolvable"] == 0

    def test_unreadable_threads_fail_closed(self) -> None:
        with self._route(decision=json.dumps({"reviewDecision": "APPROVED"}), threads="not json"):
            state = pr_reads.approval_state(repo="o/r", pr_iid=9, token="t")
        assert state["unresolved_resolvable"] == 1


class TestPrOpenState:
    def test_open(self) -> None:
        with patch.object(pr_reads, "_gh_api_get", return_value={"state": "open"}):
            assert pr_reads.pr_open_state(pr_url="https://github.com/o/r/pull/7", token="t") == PrOpenState.OPEN

    def test_merged(self) -> None:
        with patch.object(pr_reads, "_gh_api_get", return_value={"state": "closed", "merged": True}):
            assert pr_reads.pr_open_state(pr_url="https://github.com/o/r/pull/7", token="t") == PrOpenState.MERGED

    def test_unparsable_url_is_unknown(self) -> None:
        with patch.object(pr_reads, "_gh_api_get") as mock_get:
            assert pr_reads.pr_open_state(pr_url="https://gitlab.com/o/r/-/merge_requests/7", token="t") == (
                PrOpenState.UNKNOWN
            )
        mock_get.assert_not_called()

    def test_exception_fails_open_to_unknown(self) -> None:
        with patch.object(pr_reads, "_gh_api_get", side_effect=RuntimeError("boom")):
            assert pr_reads.pr_open_state(pr_url="https://github.com/o/r/pull/7", token="t") == PrOpenState.UNKNOWN


class TestPrAuthor:
    def test_returns_login(self) -> None:
        with patch.object(pr_reads, "_gh_api_get", return_value={"user": {"login": "souliane"}}):
            assert pr_reads.pr_author(pr_url="https://github.com/o/r/pull/7", token="t") == "souliane"

    def test_author_less_payload_is_empty(self) -> None:
        with patch.object(pr_reads, "_gh_api_get", return_value={"state": "open"}):
            assert pr_reads.pr_author(pr_url="https://github.com/o/r/pull/7", token="t") == ""

    def test_exception_fails_safe_to_empty(self) -> None:
        with patch.object(pr_reads, "_gh_api_get", side_effect=RuntimeError("boom")):
            assert pr_reads.pr_author(pr_url="https://github.com/o/r/pull/7", token="t") == ""
