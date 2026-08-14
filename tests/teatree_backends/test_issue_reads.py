"""Issue-level forge reads — two verdicts that must fail in OPPOSITE directions.

``issue_is_done`` gates advancing a post-ship ticket, so uncertainty must read
"not done"; ``issue_reopen_state`` gates REVIVING a delivered one, so the same
uncertainty must read UNKNOWN. These lanes pin each seam's own failure direction.
"""

from typing import cast
from unittest.mock import MagicMock

import pytest

from teatree.backends.issue_reads import issue_is_done, issue_reopen_state
from teatree.core.backend_protocols import IssueReopenState
from teatree.core.overlay import OverlayBase, OverlayConfig


def _build_overlay() -> OverlayBase:
    overlay = MagicMock(spec=OverlayBase)
    overlay.config = OverlayConfig()
    return cast("OverlayBase", overlay)


class _IssueHost:
    """A code host whose issue fetch is scripted per URL."""

    def __init__(self, payload: object, *, raises: bool = False) -> None:
        self._payload = payload
        self._raises = raises

    def get_issue(self, issue_url: str) -> object:
        _ = issue_url
        if self._raises:
            msg = "boom"
            raise RuntimeError(msg)
        return self._payload


def _done_overlay(*, verdict: bool) -> OverlayBase:
    overlay = _build_overlay()
    overlay.is_issue_done = lambda _data: verdict  # type: ignore[method-assign]
    return overlay


class TestIssueIsDone:
    """The shared completion-detection seam consumed by the sweep and the scanner."""

    def _patch_host(self, monkeypatch: pytest.MonkeyPatch, host: object | None) -> None:
        monkeypatch.setattr("teatree.backends.issue_reads.get_code_host_for_url", lambda _overlay, _url: host)

    def test_true_when_host_and_overlay_agree(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_host(monkeypatch, _IssueHost({"state": "closed"}))
        assert issue_is_done(_done_overlay(verdict=True), "https://x/1") is True

    def test_false_when_overlay_says_not_done(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_host(monkeypatch, _IssueHost({"state": "closed"}))
        assert issue_is_done(_done_overlay(verdict=False), "https://x/1") is False

    def test_false_when_no_host(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_host(monkeypatch, None)
        assert issue_is_done(_done_overlay(verdict=True), "https://x/1") is False

    def test_false_on_fetch_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_host(monkeypatch, _IssueHost(None, raises=True))
        assert issue_is_done(_done_overlay(verdict=True), "https://x/1") is False

    def test_false_on_error_payload(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_host(monkeypatch, _IssueHost({"error": "not found"}))
        assert issue_is_done(_done_overlay(verdict=True), "https://x/1") is False

    def test_false_on_non_dict_payload(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_host(monkeypatch, _IssueHost("nope"))
        assert issue_is_done(_done_overlay(verdict=True), "https://x/1") is False


class TestIssueReopenState:
    """The reopen seam rule E revives on — so every uncertainty must read UNKNOWN (#4152)."""

    def _patch_host(self, monkeypatch: pytest.MonkeyPatch, host: object | None) -> None:
        monkeypatch.setattr("teatree.backends.issue_reads.get_code_host_for_url", lambda _overlay, _url: host)

    def test_reopened_when_the_payload_says_so(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_host(monkeypatch, _IssueHost({"state": "open", "state_reason": "reopened"}))
        assert issue_reopen_state(_build_overlay(), "https://x/1") is IssueReopenState.REOPENED

    def test_an_issue_that_never_closed_is_not_reopened(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_host(monkeypatch, _IssueHost({"state": "open", "state_reason": None}))
        assert issue_reopen_state(_build_overlay(), "https://x/1") is IssueReopenState.NOT_REOPENED

    def test_unknown_when_no_host(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_host(monkeypatch, None)
        assert issue_reopen_state(_build_overlay(), "https://x/1") is IssueReopenState.UNKNOWN

    def test_unknown_on_fetch_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_host(monkeypatch, _IssueHost(None, raises=True))
        assert issue_reopen_state(_build_overlay(), "https://x/1") is IssueReopenState.UNKNOWN
