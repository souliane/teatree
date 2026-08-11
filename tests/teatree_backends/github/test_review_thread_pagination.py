"""``count_unresolved_review_threads`` walks every page of the reviewThreads connection.

GraphQL caps ``reviewThreads`` at 100 nodes per page. Reading only the first page
of a 100+-thread PR reports an unresolved thread past it as absent, so
``approval_state`` reports ``unresolved_resolvable=0`` and authorises a merge
GitHub then refuses under "Require conversation resolution before merging" — the
loop-forever symptom the hard-coded zero used to cause, reintroduced at scale.
"""

import json
import subprocess
from unittest.mock import patch

from teatree.backends.github import pr_reads


def _completed(stdout: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 0, stdout, "")


def _page(*, unresolved: int = 0, resolved: int = 0, next_cursor: str | None) -> str:
    nodes = [{"isResolved": False}] * unresolved + [{"isResolved": True}] * resolved
    page_info = {"hasNextPage": next_cursor is not None, "endCursor": next_cursor}
    return json.dumps(
        {"data": {"repository": {"pullRequest": {"reviewThreads": {"pageInfo": page_info, "nodes": nodes}}}}}
    )


class _Pages:
    """Serves the queued pages in order and records each query's ``after:`` cursor."""

    def __init__(self, pages: list[str]) -> None:
        self._pages = list(pages)
        self.cursors: list[str] = []

    def __call__(self, *args: str, **_: object) -> subprocess.CompletedProcess[str]:
        query = next(arg for arg in args if arg.startswith("query="))
        self.cursors.append(query.partition('after: "')[2].partition('"')[0])
        return _completed(self._pages.pop(0))


class TestPaginatedThreadCount:
    def test_an_unresolved_thread_on_the_second_page_is_counted(self) -> None:
        # The realistic shape: page one is 100 threads all resolved, and the one
        # still-open conversation sits past it.
        pages = _Pages(
            [_page(resolved=100, unresolved=0, next_cursor="Y3Vyc29y"), _page(unresolved=1, next_cursor=None)]
        )
        with patch.object(pr_reads, "_run_gh", side_effect=pages):
            assert pr_reads.count_unresolved_review_threads(repo="o/r", pr_iid=9, token="t") == 1
        assert pages.cursors == ["", "Y3Vyc29y"]

    def test_a_single_page_stops_after_one_read(self) -> None:
        pages = _Pages([_page(unresolved=2, next_cursor=None)])
        with patch.object(pr_reads, "_run_gh", side_effect=pages):
            assert pr_reads.count_unresolved_review_threads(repo="o/r", pr_iid=9, token="t") == 2
        assert pages.cursors == [""]

    def test_a_page_with_no_pageinfo_is_the_last_page(self) -> None:
        # An older/partial response shape must terminate the walk rather than loop.
        body = json.dumps(
            {"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": [{"isResolved": False}]}}}}}
        )
        with patch.object(pr_reads, "_run_gh", return_value=_completed(body)):
            assert pr_reads.count_unresolved_review_threads(repo="o/r", pr_iid=9, token="t") == 1

    def test_a_failing_later_page_is_unreadable_not_a_partial_count(self) -> None:
        # Half a walk is not a count: returning the 100 seen so far would under-report
        # exactly like the truncated read this walk replaces.
        pages = _Pages([_page(resolved=100, next_cursor="Y3Vyc29y"), "not json"])
        with patch.object(pr_reads, "_run_gh", side_effect=pages):
            assert pr_reads.count_unresolved_review_threads(repo="o/r", pr_iid=9, token="t") is None

    def test_a_thread_list_beyond_the_page_budget_is_unreadable(self) -> None:
        pages = _Pages([_page(resolved=100, next_cursor=f"cursor-{index}") for index in range(64)])
        with patch.object(pr_reads, "_run_gh", side_effect=pages):
            assert pr_reads.count_unresolved_review_threads(repo="o/r", pr_iid=9, token="t") is None

    def test_a_hasnextpage_with_no_usable_cursor_is_unreadable(self) -> None:
        # hasNextPage says threads exist past this read and nothing can advance to
        # them, so what was read is a partial count — the exact under-report the
        # walk exists to prevent, not a terminating page.
        pages = _Pages([_page(unresolved=3, next_cursor="")])
        with patch.object(pr_reads, "_run_gh", side_effect=pages):
            assert pr_reads.count_unresolved_review_threads(repo="o/r", pr_iid=9, token="t") is None

    def test_a_hasnextpage_with_an_absent_cursor_key_is_unreadable(self) -> None:
        body = json.dumps(
            {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "reviewThreads": {"pageInfo": {"hasNextPage": True}, "nodes": [{"isResolved": False}]}
                        }
                    }
                }
            }
        )
        with patch.object(pr_reads, "_run_gh", return_value=_completed(body)):
            assert pr_reads.count_unresolved_review_threads(repo="o/r", pr_iid=9, token="t") is None
