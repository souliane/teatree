"""Tests for the ``close_dead_issue`` mechanical handler — idempotent close (#2122, #162).

The handler resolves the code host for the issue URL and closes it through the
#162 hygiene facade: the audit rationale lands in the DESCRIPTION, where a later
reader looks, and the close itself carries NO comment — ``close_issue(comment=...)``
was a hidden comment seam. The rationale still routes through the same scanned
forge-write seam under the same ``issue_disposition_close`` audit action, so this
loop-driven close can never write to a public forge on a laxer path than the MCP
surface (CC-2).

Idempotent (the backend close is a no-op on an already-closed issue, the append is
digest-keyed) and best-effort (missing URL, unresolvable host, a leak/blocked
verdict, or a backend error never crash the tick). Two facade consequences are
pinned as inverse controls: a ticket someone ELSE filed is not closed, and a
rationale that did not land leaves the ticket OPEN.
"""

from contextlib import ExitStack
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.models import NEEDS_TRIAGE_LABEL, SendAudit
from teatree.core.overlay import OverlayBase
from teatree.loop.mechanical import close_dead_issue
from teatree.loop.scanners.issue_disposition import IssueDispositionScanner
from teatree.types import RawAPIDict
from tests._send_gate import allow_forge_repos

pytestmark = pytest.mark.usefixtures("configured_banned_term_registry")


def _slug_of(issue_url: str) -> str:
    """``owner/name`` from a forge issue URL, the way a real code host answers it."""
    parts = issue_url.split("/")
    return "/".join(parts[3:5]) if len(parts) > 5 else ""


_URL = "https://github.com/souliane/teatree/issues/900"
_SURVIVOR = "https://github.com/souliane/teatree/issues/899"
_REPO = "souliane/teatree"


_BOT = "teatree-bot"


@dataclass
class _Host:
    closed: list[tuple[str, str]] = field(default_factory=list)
    result: RawAPIDict = field(default_factory=dict)
    raise_on_close: bool = False
    issue_states: dict[str, str] = field(default_factory=dict)
    raise_on_get_issue: bool = False
    author: str = _BOT
    bodies: dict[str, str] = field(default_factory=dict)
    drop_update: bool = False

    def get_issue(self, issue_url: str) -> RawAPIDict:
        if self.raise_on_get_issue:
            raise ConnectionError
        return {
            "web_url": issue_url,
            "state": self.issue_states.get(issue_url, "open"),
            "body": self.bodies.get(issue_url, "Original body."),
            "user": {"login": self.author},
        }

    def current_user(self) -> str:
        return _BOT

    def update_issue(self, *, issue_url: str, body: str) -> RawAPIDict:
        if not self.drop_update:
            self.bodies[issue_url] = body
        return {"web_url": issue_url}

    def close_issue(self, *, issue_url: str, comment: str = "") -> RawAPIDict:
        if self.raise_on_close:
            msg = "network down"
            raise RuntimeError(msg)
        self.closed.append((issue_url, comment))
        return self.result

    def repo_for_issue_url(self, issue_url: str) -> str:
        return _slug_of(issue_url)


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
    allow_forge_repos(_REPO)
    stack.enter_context(patch("teatree.core.overlay_loader.get_overlay", return_value=_Overlay()))
    stack.enter_context(patch("teatree.backends.loader.get_code_host_for_url", return_value=host))
    stack.enter_context(patch("teatree.core.gates.privacy_gate._target_is_public", return_value=is_public))
    return stack


class CloseDeadIssueTests(TestCase):
    def test_the_rationale_lands_in_the_description_and_the_close_carries_no_comment(self) -> None:
        host = _Host()
        with _patched(host):
            close_dead_issue({"url": _URL, "reason": "already_shipped", "overlay": "acme"})
        assert host.closed == [(_URL, "")]
        assert "issue-disposition scanner" in host.bodies[_URL]
        assert "already shipped" in host.bodies[_URL]
        # The pre-existing body survives the append — this is a fold, not a replace.
        assert "Original body." in host.bodies[_URL]

    def test_the_rationale_describes_each_reason(self) -> None:
        for reason, fragment in (
            ("exact_duplicate", "duplicate"),
            ("obsolete", "obsolete"),
        ):
            host = _Host()
            with _patched(host):
                close_dead_issue({"url": _URL, "reason": reason, "duplicate_of": _SURVIVOR})
            assert fragment in host.bodies[_URL]

    def test_a_ticket_someone_else_filed_is_not_closed(self) -> None:
        """#162 Rule 5 — the factory does not retire other people's tickets."""
        host = _Host(author="someone.else")
        with _patched(host):
            close_dead_issue({"url": _URL, "reason": "already_shipped"})
        assert host.closed == []
        assert host.bodies == {}

    def test_a_rationale_that_did_not_land_leaves_the_ticket_open(self) -> None:
        """A ticket closed for a reason nobody can read is the invisibility #162 ends."""
        host = _Host(drop_update=True)
        with _patched(host):
            close_dead_issue({"url": _URL, "reason": "obsolete"})
        assert host.closed == []

    def test_a_duplicate_whose_survivor_has_closed_stays_open(self) -> None:
        host = _Host(issue_states={_SURVIVOR: "closed"})
        with _patched(host):
            close_dead_issue({"url": _URL, "reason": "exact_duplicate", "duplicate_of": _SURVIVOR})
        assert host.closed == []

    def test_a_duplicate_naming_no_survivor_stays_open(self) -> None:
        host = _Host()
        with _patched(host):
            close_dead_issue({"url": _URL, "reason": "exact_duplicate"})
        assert host.closed == []

    def test_a_duplicate_whose_survivor_cannot_be_read_stays_open(self) -> None:
        host = _Host(raise_on_get_issue=True)
        with _patched(host):
            close_dead_issue({"url": _URL, "reason": "exact_duplicate", "duplicate_of": _SURVIVOR})
        assert host.closed == []

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

    def test_a_close_exception_raises_for_the_tick_to_record(self) -> None:
        host = _Host(raise_on_close=True)
        with _patched(host), pytest.raises(RuntimeError):
            close_dead_issue({"url": _URL, "reason": "already_shipped"})

    def test_an_overlay_resolution_failure_raises_and_closes_nothing(self) -> None:
        host = _Host()
        with (
            patch("teatree.core.overlay_loader.get_overlay", side_effect=RuntimeError("no overlay")),
            patch("teatree.backends.loader.get_code_host_for_url", return_value=host),
            pytest.raises(RuntimeError, match="no overlay"),
        ):
            close_dead_issue({"url": _URL, "reason": "already_shipped"})
        assert host.closed == []


class CloseDeadIssueRoutesThroughSeamTests(TestCase):
    """The close comment routes through the scanned forge-write seam (CC-2)."""

    def test_a_clean_rationale_writes_a_send_audit_row_under_the_handler_action(self) -> None:
        host = _Host()
        with (
            _patched(host, is_public=True),
            patch("teatree.core.gates.privacy_gate.overlay_privacy_rules", return_value=([], [])),
        ):
            close_dead_issue({"url": _URL, "reason": "already_shipped"})
        assert len(host.closed) == 1
        # The facade carries the caller's own audit label, so the row still names THIS writer.
        row = SendAudit.objects.get(action="issue_disposition_close")
        assert row.destination == f"github:{_REPO}"
        assert row.target == _URL

    def test_a_leaking_rationale_to_a_public_repo_is_refused_and_the_close_skipped(self) -> None:
        host = _Host()
        # The disposition reason is scanner-controlled, but a redact term in it
        # must still be caught by the seam before it reaches a public forge.
        with (
            _patched(host, is_public=True),
            patch("teatree.core.gates.privacy_gate.overlay_privacy_rules", return_value=(["exact"], [])),
        ):
            close_dead_issue({"url": _URL, "reason": "exact_duplicate", "duplicate_of": _SURVIVOR})
        # The leaking rationale is never written and the close is skipped.
        assert host.closed == []
        assert host.bodies == {}


@dataclass
class _Tracker:
    """One issue tracker answering the scanner's reads and the handler's closes from the same open set."""

    title: str
    open_urls: list[str]

    bodies: dict[str, str] = field(default_factory=dict)

    def _issue(self, url: str) -> RawAPIDict:
        state = "open" if url in self.open_urls else "closed"
        return {
            "web_url": url,
            "title": self.title,
            "body": self.bodies.get(url, ""),
            "state": state,
            "labels": [NEEDS_TRIAGE_LABEL],
            "user": {"login": "alice"},
        }

    def current_user(self) -> str:
        return "alice"

    def update_issue(self, *, issue_url: str, body: str) -> RawAPIDict:
        self.bodies[issue_url] = body
        return {"web_url": issue_url}

    def list_assigned_issues(self, *, assignee: str, repo_slugs: tuple[str, ...] = ()) -> list[RawAPIDict]:
        _ = (assignee, repo_slugs)
        return [self._issue(url) for url in self.open_urls]

    def search_open_issues(self, *, repo: str, query: str) -> list[RawAPIDict]:
        _ = query
        return [self._issue(url) for url in self.open_urls if _slug_of(url) == repo]

    def get_issue(self, issue_url: str) -> RawAPIDict:
        return self._issue(issue_url)

    def close_issue(self, *, issue_url: str, comment: str = "") -> RawAPIDict:
        _ = comment
        if issue_url in self.open_urls:
            self.open_urls.remove(issue_url)
        return {}

    def repo_for_issue_url(self, issue_url: str) -> str:
        return _slug_of(issue_url)


class DuplicateGroupsKeepTheirOldestIssueTests(TestCase):
    """A whole tick — scan, then close every candidate — must leave one open issue per duplicate group."""

    BASE = "https://github.com/souliane/teatree/issues"

    def _tick(self, tracker: _Tracker) -> None:
        scanner = IssueDispositionScanner(host=tracker, overlay_name="acme")
        with _patched(tracker):
            for signal in scanner.scan():
                close_dead_issue(signal.payload)

    def test_two_duplicates_close_the_newer_and_a_later_tick_closes_nothing(self) -> None:
        tracker = _Tracker(title="Fix the broken login flow", open_urls=[f"{self.BASE}/10", f"{self.BASE}/11"])

        self._tick(tracker)
        assert tracker.open_urls == [f"{self.BASE}/10"]

        self._tick(tracker)
        assert tracker.open_urls == [f"{self.BASE}/10"]

    def test_three_duplicates_leave_the_oldest_open(self) -> None:
        tracker = _Tracker(
            title="Fix the broken login flow",
            open_urls=[f"{self.BASE}/12", f"{self.BASE}/10", f"{self.BASE}/11"],
        )

        self._tick(tracker)

        assert tracker.open_urls == [f"{self.BASE}/10"]


class DuplicatesNeverCrossARepoBoundaryTests(TestCase):
    """A whole tick over a TWO-repo listing must close nothing on a title match alone.

    The listing spans every owned repo, so two repos can carry the same title with no
    relationship at all — and a bare issue number is not comparable across them.
    """

    A_URL = "https://github.com/souliane/repo-a/issues/10"
    B_URL = "https://github.com/souliane/repo-b/issues/20"

    def test_a_shared_title_across_two_owned_repos_closes_nothing(self) -> None:
        tracker = _Tracker(title="Fix the broken login flow", open_urls=[self.A_URL, self.B_URL])
        scanner = IssueDispositionScanner(host=tracker, overlay_name="acme")

        with _patched(tracker):
            for signal in scanner.scan():
                close_dead_issue(signal.payload)

        assert tracker.open_urls == [self.A_URL, self.B_URL]

    def test_a_cross_repo_duplicate_payload_closes_nothing(self) -> None:
        # The handler is the last guard: even handed a cross-repo pairing directly, it holds.
        host = _Host()
        with _patched(host):
            close_dead_issue({"url": self.B_URL, "reason": "exact_duplicate", "duplicate_of": self.A_URL})

        assert host.closed == []
