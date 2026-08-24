"""The incremental sync watermark must never ratchet past an unread window.

``LAST_SYNC_CACHE_KEY`` is monotonic: once it advances to T, no later sync ever
asks the forge about anything before T. Advancing it after a failed
``last_sync``-bounded fetch therefore retires that window permanently — an MR
that merged inside it is never seen again, so its ticket never reaches MERGED
and its worktrees are never cleaned.
"""

from collections.abc import Iterator
from unittest.mock import MagicMock

import httpx
import pytest
from django.core.cache import cache
from django.test import TestCase

from teatree.backends.gitlab.sync_terminal import detect_closed_prs, detect_merged_prs
from teatree.core.models import Ticket
from teatree.core.sync import sync_followup
from teatree.types import LAST_SYNC_CACHE_KEY, RawAPIDict, SyncResult
from tests.teatree_core.sync._overlays import SyncOverlay, _make_mock_client, _patch_overlay

_WATERMARK = "2020-01-01T00:00:00+00:00"
_MERGED_INSIDE_WINDOW = "2020-01-01T01:00:00+00:00"
_MR_URL = "https://gitlab.com/org/repo/-/merge_requests/42"
_MERGED_MR: RawAPIDict = {"web_url": _MR_URL, "iid": 42, "project_id": 123}


class _WindowedMergedFetcher:
    """A server-side ``updated_after`` window, plus a scripted outage.

    Mirrors what GitLab does: an MR merged at ``_MERGED_INSIDE_WINDOW`` is
    returned only while the caller's cutoff is at or before that instant. A
    watermark that advanced past the outage puts the cutoff beyond it, and the
    merge becomes unreachable.
    """

    def __init__(self, *, fail_first: bool) -> None:
        self.fail_first = fail_first
        self.cutoffs: list[str | None] = []

    def __call__(self, _username: str, *, updated_after: str | None = None) -> list[dict[str, object]]:
        self.cutoffs.append(updated_after)
        if self.fail_first and len(self.cutoffs) == 1:
            msg = "502 Bad Gateway"
            raise httpx.HTTPError(msg)
        if updated_after is not None and updated_after > _MERGED_INSIDE_WINDOW:
            return []
        return [_MERGED_MR]


class TestTerminalDetectionReportsWhetherItReadTheWindow(TestCase):
    """An empty window and a failed read are different outcomes, reported apart."""

    def test_merged_detection_reports_read_on_an_empty_window(self) -> None:
        client = MagicMock()
        client.list_recently_merged_mrs.return_value = []
        result = SyncResult()

        assert detect_merged_prs(client, "testuser", result, None) is True
        assert result.errors == []

    def test_merged_detection_reports_unread_on_a_failed_fetch(self) -> None:
        client = MagicMock()
        client.list_recently_merged_mrs.side_effect = httpx.HTTPError("502 Bad Gateway")
        result = SyncResult()

        assert detect_merged_prs(client, "testuser", result, None) is False
        assert result.errors == ["Merged PR fetch failed: 502 Bad Gateway"]

    def test_closed_detection_reports_read_on_an_empty_window(self) -> None:
        client = MagicMock()
        client.list_recently_closed_mrs.return_value = []

        assert detect_closed_prs(client, "testuser", SyncResult(), None) is True

    def test_closed_detection_reports_unread_on_a_failed_fetch(self) -> None:
        client = MagicMock()
        client.list_recently_closed_mrs.side_effect = httpx.HTTPError("502 Bad Gateway")
        result = SyncResult()

        assert detect_closed_prs(client, "testuser", result, None) is False
        assert result.errors == ["Closed PR fetch failed: 502 Bad Gateway"]


class TestWatermarkHeldOnFailedIncrementalFetch(TestCase):
    _OVERLAY = SyncOverlay()

    @pytest.fixture(autouse=True)
    def _with_overlay(self, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
        self._monkeypatch = monkeypatch
        cache.delete(LAST_SYNC_CACHE_KEY)
        with _patch_overlay(self._OVERLAY):
            yield
        cache.delete(LAST_SYNC_CACHE_KEY)

    def _install(self, mock_client: MagicMock) -> None:
        self._monkeypatch.setattr("teatree.backends.gitlab.api.GitLabAPI", lambda **_kw: mock_client)

    def test_merged_fetch_failure_holds_the_watermark(self) -> None:
        cache.set(LAST_SYNC_CACHE_KEY, _WATERMARK, timeout=None)
        mock_client = _make_mock_client([])
        mock_client.list_recently_merged_mrs.side_effect = httpx.HTTPError("502 Bad Gateway")
        self._install(mock_client)

        result = sync_followup()

        assert any("Merged PR fetch failed" in e for e in result.errors)
        assert cache.get(LAST_SYNC_CACHE_KEY) == _WATERMARK

    def test_closed_fetch_failure_holds_the_watermark(self) -> None:
        cache.set(LAST_SYNC_CACHE_KEY, _WATERMARK, timeout=None)
        mock_client = _make_mock_client([])
        mock_client.list_recently_closed_mrs.side_effect = httpx.HTTPError("502 Bad Gateway")
        self._install(mock_client)

        result = sync_followup()

        assert any("Closed PR fetch failed" in e for e in result.errors)
        assert cache.get(LAST_SYNC_CACHE_KEY) == _WATERMARK

    def test_fully_successful_sync_advances_the_watermark(self) -> None:
        cache.set(LAST_SYNC_CACHE_KEY, _WATERMARK, timeout=None)
        self._install(_make_mock_client([]))

        result = sync_followup()

        assert result.errors == []
        assert cache.get(LAST_SYNC_CACHE_KEY) != _WATERMARK

    def test_merge_inside_the_failed_window_is_still_applied_on_the_next_sync(self) -> None:
        """The end-to-end property: a skipped window loses the merge forever."""
        cache.set(LAST_SYNC_CACHE_KEY, _WATERMARK, timeout=None)
        ticket = Ticket.objects.create(
            overlay="test",
            issue_url="https://gitlab.com/org/repo/-/issues/100",
            repos=["repo"],
            state=Ticket.State.IN_REVIEW,
            extra={"prs": {_MR_URL: {"title": "MR42"}}},
        )
        fetcher = _WindowedMergedFetcher(fail_first=True)
        mock_client = _make_mock_client([])
        mock_client.list_recently_merged_mrs.side_effect = fetcher
        self._install(mock_client)

        sync_followup()
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.IN_REVIEW

        sync_followup()

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.MERGED
        assert fetcher.cutoffs == [_WATERMARK, _WATERMARK]
