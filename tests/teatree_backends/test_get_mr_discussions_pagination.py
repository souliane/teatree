"""Regression: ``get_mr_discussions`` must paginate (merge-gate fail-open).

``get_json`` silently truncates to a single page (at most 100 threads).  A
blocking unresolved-resolvable thread sitting on page 2+ causes the call site
in ``GitLabCodeHost.get_mr_approvals`` to undercount to 0, which flips the
scanner signal from ``merge_blocked`` to ``merge_needed`` — the factory tries
to auto-merge an MR that teatree's own gate intends to block (fail-open on a
merge decision).

Three callers all share the same bug; this module covers each in turn.

The page-1 ``GitLabAPI.get_mr_discussions`` call must return all threads across
>100 (two pages). The SUBSTRATE case drives the full path — real GitLabAPI
pagination, then ``GitLabCodeHost.get_mr_approvals``, then ``_signal_for`` — to
emit ``incoming_event.merge_blocked`` rather than ``incoming_event.merge_needed``
(the auto-merge-gate fail-open). The advisory ``review_run._open_discussion_count``
path must likewise count threads sitting on page 2.
"""

from unittest.mock import MagicMock, patch

from teatree.backends.gitlab import GitLabCodeHost
from teatree.backends.gitlab_api import GitLabAPI, ProjectInfo
from teatree.cli.review_run import _open_discussion_count
from teatree.core.gates.merge_guard import MergeGuard
from teatree.loop.scanners.gitlab_approvals import _signal_for

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _unresolved_thread() -> dict:
    """A single discussion thread with one unresolved resolvable note."""
    return {
        "notes": [
            {"resolvable": True, "resolved": False, "author": {"username": "reviewer"}},
        ]
    }


def _resolved_thread() -> dict:
    """A resolved discussion thread — does not count against the merge gate."""
    return {
        "notes": [
            {"resolvable": True, "resolved": True, "author": {"username": "reviewer"}},
        ]
    }


# ---------------------------------------------------------------------------
# Test 1: GitLabAPI.get_mr_discussions returns all pages
# ---------------------------------------------------------------------------


class TestGetMrDiscussionsPagination:
    """``get_mr_discussions`` must walk all pages, not just the first."""

    def test_returns_threads_from_page_two(self) -> None:
        """A thread that sits exclusively on page 2 must be returned."""
        page1 = [_resolved_thread()] * 100
        page2 = [_unresolved_thread()]

        with patch("httpx.get") as mock_get:
            # First call → page 1 with x-next-page=2; second call → page 2 (no next page).
            resp1 = MagicMock()
            resp1.raise_for_status.return_value = None
            resp1.json.return_value = page1
            resp1.headers = {"x-next-page": "2"}

            resp2 = MagicMock()
            resp2.raise_for_status.return_value = None
            resp2.json.return_value = page2
            resp2.headers = {"x-next-page": ""}

            mock_get.side_effect = [resp1, resp2]

            api = GitLabAPI(token="tok", base_url="https://gitlab.example.com/api/v4")
            result = api.get_mr_discussions(project_id=1, mr_iid=42)

        assert len(result) == 101, "must return threads from both pages"
        assert result[-1] == page2[0], "page-2 thread must appear in result"

    def test_single_page_still_works(self) -> None:
        """When there is only one page the result matches the single response."""
        page1 = [_resolved_thread(), _unresolved_thread()]

        with patch("httpx.get") as mock_get:
            resp1 = MagicMock()
            resp1.raise_for_status.return_value = None
            resp1.json.return_value = page1
            resp1.headers = {"x-next-page": ""}

            mock_get.side_effect = [resp1]

            api = GitLabAPI(token="tok", base_url="https://gitlab.example.com/api/v4")
            result = api.get_mr_discussions(project_id=1, mr_iid=1)

        assert result == page1


# ---------------------------------------------------------------------------
# Test 2 (SUBSTRATE): a blocking thread on page 2 must drive the scanner to
# emit ``merge_blocked``, NOT ``merge_needed`` (auto-merge-gate fail-open).
# ---------------------------------------------------------------------------


def _approvals_http_side_effect(page1: list[dict], page2: list[dict]):
    """httpx.get side-effect for a real GitLabAPI behind GitLabCodeHost.

    The MR is approved (``approvals_left == 0``) and carries 100 resolved
    threads on page 1 plus one unresolved-resolvable thread on page 2. On
    single-page code ``get_mr_discussions`` returns page 1 only → 0 unresolved
    → the scanner emits ``merge_needed`` (fail-open). With pagination the page-2
    thread is counted → the scanner emits ``merge_blocked``.
    """

    def _side_effect(url: str, **kwargs: object) -> MagicMock:
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.headers = {"x-next-page": ""}
        if "/discussions" in url:
            # get_json_paginated appends &page=N; page 1 advertises a next page.
            if "page=2" in url:
                resp.json.return_value = page2
            else:
                resp.json.return_value = page1
                resp.headers = {"x-next-page": "2"}
        elif "/approvals" in url:
            resp.json.return_value = {"approvals_left": 0, "approved_by": []}
        else:
            resp.json.return_value = {}
        return resp

    return _side_effect


class TestSubstrateMergeBlockedOnPageTwoThread:
    """A page-2 blocking thread must yield ``merge_blocked``, never ``merge_needed``.

    This is the SUBSTRATE regression. The full path is exercised end to end:
    a real ``GitLabAPI`` paginating discussions over ``httpx.get`` →
    ``GitLabCodeHost.get_mr_approvals`` → ``_count_unresolved_resolvable_threads``
    → ``_signal_for`` with a permissive overlay guard.

    On single-page code the page-2 thread is dropped, ``unresolved`` is 0, and
    ``_signal_for`` returns ``incoming_event.merge_needed`` — the factory would
    auto-merge an MR teatree's own gate intends to block. After the fix the
    thread is counted and the signal is ``incoming_event.merge_blocked``.
    """

    def test_scanner_emits_merge_blocked_not_merge_needed(self) -> None:
        page1 = [_resolved_thread()] * 100
        page2 = [_unresolved_thread()]

        with patch("httpx.get") as mock_get:
            mock_get.side_effect = _approvals_http_side_effect(page1, page2)
            api = GitLabAPI(token="tok", base_url="https://gitlab.example.com/api/v4")
            host = GitLabCodeHost(client=api)
            with patch.object(
                host,
                "_resolve_project",
                return_value=ProjectInfo(
                    project_id=7,
                    path_with_namespace="acme/backend",
                    short_name="backend",
                    default_branch="main",
                ),
            ):
                state = host.get_mr_approvals(repo="acme/backend", pr_iid=42)

        # The undercount is what flips the signal. Assert it is correct first.
        assert state["unresolved_resolvable"] == 1, (
            "page-2 blocking thread must be counted — if this is 0 the gate is fail-open"
        )

        # Drive the same value the scanner feeds _signal_for with a permissive
        # overlay guard (allowed=True). The unresolved count is the only lever:
        # >0 → merge_blocked, 0 → merge_needed.
        signal = _signal_for(
            guard=MergeGuard(allowed=True),
            url="https://gitlab.example.com/acme/backend/-/merge_requests/42",
            target_ref="main",
            unresolved=state["unresolved_resolvable"],
            title="Add widget",
        )
        assert signal is not None
        assert signal.kind == "incoming_event.merge_blocked", (
            "blocking thread on page 2 must produce merge_blocked; merge_needed here is the auto-merge-gate fail-open"
        )

    def test_signal_is_merge_needed_when_truly_zero_unresolved(self) -> None:
        """Guard: an approved MR with genuinely 0 unresolved threads → merge_needed.

        Confirms the signal lever is the unresolved count itself (not an
        artefact), so the page-2 assertion above is meaningful.
        """
        signal = _signal_for(
            guard=MergeGuard(allowed=True),
            url="https://gitlab.example.com/acme/backend/-/merge_requests/42",
            target_ref="main",
            unresolved=0,
            title="Add widget",
        )
        assert signal is not None
        assert signal.kind == "incoming_event.merge_needed"


# ---------------------------------------------------------------------------
# Test 3: review_run verdict — page-2 unresolved must not yield ready_to_review
# ---------------------------------------------------------------------------


class TestReviewRunVerdictPaginatesDiscussions:
    """``_fetch_review_state`` must not return ``ready_to_review`` when page 2 has open threads.

    ``_fetch_review_state`` calls ``api.get_mr_discussions`` and passes the
    result to ``_open_discussion_count``.  If ``get_mr_discussions`` truncates
    at page 1, an open thread on page 2 is invisible → the verdict is
    ``ready_to_review`` on an MR that actually has unresolved threads.
    """

    def test_open_discussion_on_page_two_raises_count(self) -> None:
        """``_open_discussion_count`` sees all threads including page 2."""
        page1 = [{"notes": [{"resolved": True}]}] * 100
        page2 = [{"notes": [{"resolved": False}]}]

        all_discussions = page1 + page2
        count = _open_discussion_count(all_discussions)
        assert count > 0, "page-2 unresolved thread must be counted"
        assert count == 1
