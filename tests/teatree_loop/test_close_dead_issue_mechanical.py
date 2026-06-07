"""Tests for the ``close_dead_issue`` mechanical handler — idempotent close (#2122).

The handler resolves the code host for the issue URL and closes it with an
audit-trail comment. Idempotent (the backend close is a no-op on an
already-closed issue) and best-effort (missing URL, unresolvable host, backend
error never crash the tick). It labels/closes only — never creates a Task/claim.
"""

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch

from django.test import TestCase

from teatree.core.overlay import OverlayBase
from teatree.loop.mechanical import close_dead_issue
from teatree.types import RawAPIDict

_URL = "https://github.com/souliane/teatree/issues/900"


@dataclass
class _Host:
    closed: list[tuple[str, str]] = field(default_factory=list)
    result: RawAPIDict = field(default_factory=dict)
    raise_on_close: bool = False

    def close_issue(self, *, issue_url: str, comment: str = "") -> RawAPIDict:
        if self.raise_on_close:
            msg = "network down"
            raise RuntimeError(msg)
        self.closed.append((issue_url, comment))
        return self.result


class _Overlay(OverlayBase):
    def get_repos(self) -> list[str]:
        return ["acme-repo"]

    def get_provision_steps(self, worktree: Any) -> list:
        _ = worktree
        return []


def _patch(host: _Host | None):
    return (
        patch("teatree.core.overlay_loader.get_overlay", return_value=_Overlay()),
        patch("teatree.backends.loader.get_code_host_for_url", return_value=host),
    )


class CloseDeadIssueTests(TestCase):
    def test_closes_issue_with_audit_comment(self) -> None:
        host = _Host()
        overlay_patch, host_patch = _patch(host)
        with overlay_patch, host_patch:
            close_dead_issue({"url": _URL, "reason": "already_shipped", "overlay": "acme"})
        assert len(host.closed) == 1
        closed_url, comment = host.closed[0]
        assert closed_url == _URL
        assert "issue-disposition scanner" in comment
        assert "already shipped" in comment

    def test_audit_comment_describes_each_reason(self) -> None:
        for reason, fragment in (
            ("exact_duplicate", "duplicate"),
            ("obsolete", "obsolete"),
        ):
            host = _Host()
            overlay_patch, host_patch = _patch(host)
            with overlay_patch, host_patch:
                close_dead_issue({"url": _URL, "reason": reason})
            assert fragment in host.closed[0][1]

    def test_missing_url_no_ops(self) -> None:
        host = _Host()
        overlay_patch, host_patch = _patch(host)
        with overlay_patch, host_patch:
            close_dead_issue({"reason": "already_shipped"})  # must not raise
        assert host.closed == []

    def test_unresolvable_host_no_ops(self) -> None:
        overlay_patch, host_patch = _patch(None)
        with overlay_patch, host_patch:
            close_dead_issue({"url": _URL, "reason": "already_shipped"})  # must not raise

    def test_backend_error_payload_is_swallowed(self) -> None:
        host = _Host(result={"error": "could not resolve project"})
        overlay_patch, host_patch = _patch(host)
        with overlay_patch, host_patch:
            close_dead_issue({"url": _URL, "reason": "obsolete"})  # must not raise
        assert len(host.closed) == 1

    def test_close_exception_is_swallowed(self) -> None:
        host = _Host(raise_on_close=True)
        overlay_patch, host_patch = _patch(host)
        with overlay_patch, host_patch:
            close_dead_issue({"url": _URL, "reason": "already_shipped"})  # must not raise

    def test_overlay_resolution_failure_no_ops(self) -> None:
        host = _Host()
        with (
            patch("teatree.core.overlay_loader.get_overlay", side_effect=RuntimeError("no overlay")),
            patch("teatree.backends.loader.get_code_host_for_url", return_value=host),
        ):
            close_dead_issue({"url": _URL, "reason": "already_shipped"})  # must not raise
        assert host.closed == []
