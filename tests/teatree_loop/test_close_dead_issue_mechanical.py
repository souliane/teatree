"""Tests for the ``close_dead_issue`` mechanical handler — idempotent close (#2122).

The handler resolves the code host for the issue URL and closes it with an
audit-trail comment. The comment routes through the SAME scanned forge-write seam
the MCP ``<forge>_issue_close`` twin uses (public-repo leak gate + #117
send-proxy), so this loop-driven close can never post to a public forge on a
laxer path than the MCP surface (CC-2). Idempotent (the backend close is a no-op
on an already-closed issue) and best-effort (missing URL, unresolvable host, a
leak/blocked scrub verdict, or a backend error never crash the tick). It
labels/closes only — never creates a Task/claim.
"""

from contextlib import ExitStack
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch

from django.test import TestCase

from teatree.core.models import SendAudit
from teatree.core.overlay import OverlayBase
from teatree.loop.mechanical import close_dead_issue
from teatree.types import RawAPIDict

_URL = "https://github.com/souliane/teatree/issues/900"
_REPO = "souliane/teatree"


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

    def repo_for_issue_url(self, issue_url: str) -> str:
        _ = issue_url
        return _REPO


class _Overlay(OverlayBase):
    def get_repos(self) -> list[str]:
        return ["acme-repo"]

    def get_provision_steps(self, worktree: Any) -> list:
        _ = worktree
        return []


def _patched(host: _Host | None, *, is_public: bool = False) -> ExitStack:
    """Patch the overlay, host resolver, and the leak-scan visibility probe.

    The probe is pinned (default PRIVATE = clean pass) so the transport-mechanics
    tests never shell out to ``gh``/``glab``; the leak-scan test flips it to
    PUBLIC to exercise the refusal path.
    """
    stack = ExitStack()
    stack.enter_context(patch("teatree.core.overlay_loader.get_overlay", return_value=_Overlay()))
    stack.enter_context(patch("teatree.backends.loader.get_code_host_for_url", return_value=host))
    stack.enter_context(patch("teatree.core.gates.privacy_gate._target_is_public", return_value=is_public))
    return stack


class CloseDeadIssueTests(TestCase):
    def test_closes_issue_with_audit_comment(self) -> None:
        host = _Host()
        with _patched(host):
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
            with _patched(host):
                close_dead_issue({"url": _URL, "reason": reason})
            assert fragment in host.closed[0][1]

    def test_missing_url_no_ops(self) -> None:
        host = _Host()
        with _patched(host):
            close_dead_issue({"reason": "already_shipped"})  # must not raise
        assert host.closed == []

    def test_unresolvable_host_no_ops(self) -> None:
        with _patched(None):
            close_dead_issue({"url": _URL, "reason": "already_shipped"})  # must not raise

    def test_backend_error_payload_is_swallowed(self) -> None:
        host = _Host(result={"error": "could not resolve project"})
        with _patched(host):
            close_dead_issue({"url": _URL, "reason": "obsolete"})  # must not raise
        assert len(host.closed) == 1

    def test_close_exception_is_swallowed(self) -> None:
        host = _Host(raise_on_close=True)
        with _patched(host):
            close_dead_issue({"url": _URL, "reason": "already_shipped"})  # must not raise

    def test_overlay_resolution_failure_no_ops(self) -> None:
        host = _Host()
        with (
            patch("teatree.core.overlay_loader.get_overlay", side_effect=RuntimeError("no overlay")),
            patch("teatree.backends.loader.get_code_host_for_url", return_value=host),
        ):
            close_dead_issue({"url": _URL, "reason": "already_shipped"})  # must not raise
        assert host.closed == []


class CloseDeadIssueRoutesThroughSeamTests(TestCase):
    """The close comment routes through the scanned forge-write seam (CC-2)."""

    def test_clean_close_comment_writes_a_send_audit_row(self) -> None:
        host = _Host()
        with (
            _patched(host, is_public=True),
            patch("teatree.core.gates.privacy_gate._overlay_privacy_rules", return_value=([], [])),
        ):
            close_dead_issue({"url": _URL, "reason": "already_shipped"})
        assert len(host.closed) == 1
        row = SendAudit.objects.get(action="issue_disposition_close")
        assert row.destination == _REPO
        assert row.target == _URL

    def test_leaking_close_comment_to_a_public_repo_is_refused_and_close_skipped(self) -> None:
        host = _Host()
        # The disposition reason is scanner-controlled, but a redact term in it
        # must still be caught by the seam before the comment reaches a public forge.
        with (
            _patched(host, is_public=True),
            patch("teatree.core.gates.privacy_gate._overlay_privacy_rules", return_value=(["exact"], [])),
        ):
            close_dead_issue({"url": _URL, "reason": "exact_duplicate"})  # must not raise
        # The leaking comment is never posted and the close is skipped.
        assert host.closed == []
