"""Tests for the loop scanners — pure-Python signal collectors."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from django.test import TestCase

from teatree.core.backend_protocols import PrOpenState, ReviewState
from teatree.core.models import Ticket
from teatree.loop.scanners.my_prs import MyPrsScanner
from teatree.loop.scanners.notion_view import NotionViewScanner
from teatree.loop.scanners.pr_payload import head_sha
from teatree.loop.scanners.reviewer_prs import CacheEntry, ReviewerPrsScanner, _persist_entry
from teatree.loop.scanners.reviewer_prs import mark_reviewed as _mark_reviewed_helper
from teatree.loop.scanners.slack_mentions import SlackMentionsScanner
from teatree.types import RawAPIDict


@dataclass
class FakeCodeHost:
    """In-memory CodeHostBackend conforming to the protocol — no MagicMock(spec=)."""

    user: str = ""
    my_prs: list[RawAPIDict] = field(default_factory=list)
    review_requested_prs: list[RawAPIDict] = field(default_factory=list)
    assigned_issues: list[RawAPIDict] = field(default_factory=list)
    list_assigned_issues_calls: list[str] = field(default_factory=list)
    review_state_by_url: dict[str, ReviewState] = field(default_factory=dict)
    get_review_state_calls: list[tuple[str, str]] = field(default_factory=list)
    pr_open_state_by_url: dict[str, PrOpenState] = field(default_factory=dict)
    pr_open_state_default: PrOpenState = PrOpenState.UNKNOWN
    get_pr_open_state_calls: list[str] = field(default_factory=list)

    def current_user(self) -> str:
        return self.user

    def list_my_prs(self, *, author: str, updated_after: str | None = None) -> list[RawAPIDict]:
        _ = (author, updated_after)
        return self.my_prs

    def list_review_requested_prs(self, *, reviewer: str, updated_after: str | None = None) -> list[RawAPIDict]:
        _ = (reviewer, updated_after)
        return self.review_requested_prs

    def list_assigned_issues(self, *, assignee: str) -> list[RawAPIDict]:
        self.list_assigned_issues_calls.append(assignee)
        return self.assigned_issues

    def get_review_state(self, *, pr_url: str, reviewer: str) -> ReviewState:
        self.get_review_state_calls.append((pr_url, reviewer))
        return self.review_state_by_url.get(pr_url, ReviewState.NONE)

    def get_pr_open_state(self, *, pr_url: str) -> PrOpenState:
        self.get_pr_open_state_calls.append(pr_url)
        return self.pr_open_state_by_url.get(pr_url, self.pr_open_state_default)

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


@dataclass
class FakeMessaging:
    """In-memory MessagingBackend conforming to the protocol."""

    mentions: list[RawAPIDict] = field(default_factory=list)
    dms: list[RawAPIDict] = field(default_factory=list)

    def fetch_mentions(self, *, since: str = "") -> list[RawAPIDict]:
        _ = since
        return self.mentions

    def fetch_dms(self, *, since: str = "") -> list[RawAPIDict]:
        _ = since
        return self.dms

    def post_message(self, *, channel: str, text: str, thread_ts: str = "") -> RawAPIDict:
        _ = (channel, text, thread_ts)
        return {}

    def post_reply(self, *, channel: str, ts: str, text: str) -> RawAPIDict:
        _ = (channel, ts, text)
        return {}

    def react(self, *, channel: str, ts: str, emoji: str) -> RawAPIDict:
        _ = (channel, ts, emoji)
        return {}

    def resolve_user_id(self, handle: str) -> str:
        _ = handle
        return ""


class TestMyPrsScanner:
    def test_returns_empty_when_user_unknown(self) -> None:
        host = FakeCodeHost(user="")
        assert MyPrsScanner(host=host).scan() == []

    def test_failed_pipeline_yields_action_needed_signal(self) -> None:
        host = FakeCodeHost(
            user="alice",
            my_prs=[
                {
                    "iid": 7,
                    "title": "Fix thing",
                    "web_url": "https://gitlab/x/-/merge_requests/7",
                    "head_pipeline": {"status": "failed"},
                }
            ],
        )
        signals = MyPrsScanner(host=host).scan()
        assert [s.kind for s in signals] == ["my_pr.failed"]
        assert "Fix thing" in signals[0].summary

    def test_unresolved_notes_yields_draft_notes_signal(self) -> None:
        host = FakeCodeHost(
            user="alice",
            my_prs=[{"iid": 8, "title": "WIP", "web_url": "x", "user_notes_count": 3}],
        )
        signals = MyPrsScanner(host=host).scan()
        assert [s.kind for s in signals] == ["my_pr.draft_notes"]

    def test_clean_pr_yields_open_signal(self) -> None:
        host = FakeCodeHost(
            user="alice",
            my_prs=[{"iid": 9, "title": "Done", "web_url": "x", "head_pipeline": {"status": "success"}}],
        )
        signals = MyPrsScanner(host=host).scan()
        assert [s.kind for s in signals] == ["my_pr.open"]

    def test_github_status_check_rollup_used_for_pipeline_state(self) -> None:
        host = FakeCodeHost(
            user="alice",
            my_prs=[
                {
                    "number": 11,
                    "title": "GH PR",
                    "html_url": "https://github.com/o/r/pull/11",
                    "status_check_rollup": {"state": "failure"},
                }
            ],
        )
        signals = MyPrsScanner(host=host).scan()
        assert [s.kind for s in signals] == ["my_pr.failed"]

    def test_mergeable_state_string_used_when_rollup_missing(self) -> None:
        host = FakeCodeHost(
            user="alice",
            my_prs=[
                {
                    "number": 12,
                    "title": "GH PR",
                    "html_url": "https://github.com/o/r/pull/12",
                    "mergeable_state": "error",
                }
            ],
        )
        signals = MyPrsScanner(host=host).scan()
        assert [s.kind for s in signals] == ["my_pr.failed"]

    def test_pr_without_url_or_title_still_emits_open_signal(self) -> None:
        """No pipeline yet (blank status) = legitimately not-started, not red."""
        host = FakeCodeHost(
            user="alice",
            my_prs=[{"iid": 0, "title": None, "web_url": None}],
        )
        signals = MyPrsScanner(host=host).scan()
        assert [s.kind for s in signals] == ["my_pr.open"]
        assert signals[0].payload["url"] == ""
        assert signals[0].payload["title"] == ""

    @pytest.mark.parametrize(
        "status",
        ["canceled", "cancelled", "skipped", "manual", "blocked", "stale", "neutral", "action_required"],
    )
    def test_non_green_terminal_status_is_treated_as_failed(self, status: str) -> None:
        """Not-green == red.

        A pipeline that is not ``success`` and not legitimately
        in-progress (canceled / skipped / manual-not-run / any unknown
        terminal state) must surface as action-needed, never silently as
        a benign open PR.
        """
        host = FakeCodeHost(
            user="alice",
            my_prs=[
                {
                    "iid": 21,
                    "title": "Gray pipeline",
                    "web_url": "x",
                    "head_pipeline": {"status": status},
                }
            ],
        )
        signals = MyPrsScanner(host=host).scan()
        assert [s.kind for s in signals] == ["my_pr.failed"], status

    @pytest.mark.parametrize("status", ["running", "pending", "created", "preparing", "scheduled"])
    def test_in_progress_status_is_not_treated_as_failed(self, status: str) -> None:
        """A pipeline still legitimately running is not yet red."""
        host = FakeCodeHost(
            user="alice",
            my_prs=[
                {
                    "iid": 22,
                    "title": "Still running",
                    "web_url": "x",
                    "head_pipeline": {"status": status},
                }
            ],
        )
        signals = MyPrsScanner(host=host).scan()
        assert [s.kind for s in signals] == ["my_pr.open"], status


class TestReviewerPrsScanner(TestCase):
    def test_unreviewed_first_pass_emits_signal(self) -> None:
        host = FakeCodeHost(
            user="alice",
            review_requested_prs=[{"web_url": "https://gitlab/x/-/merge_requests/3", "sha": "abc"}],
        )
        scanner = ReviewerPrsScanner(host=host)
        signals = scanner.scan()
        assert [s.kind for s in signals] == ["reviewer_pr.unreviewed"]

    def test_new_sha_after_review_emits_new_sha(self) -> None:
        host = FakeCodeHost(
            user="alice",
            review_requested_prs=[{"web_url": "https://gitlab/x/-/merge_requests/3", "sha": "newer"}],
        )
        scanner = ReviewerPrsScanner(host=host)
        _mark_reviewed_helper(url="https://gitlab/x/-/merge_requests/3", sha="older")
        signals = scanner.scan()
        assert [s.kind for s in signals] == ["reviewer_pr.new_sha"]

    def test_already_reviewed_emits_nothing(self) -> None:
        host = FakeCodeHost(
            user="alice",
            review_requested_prs=[{"web_url": "https://gitlab/x/-/merge_requests/3", "sha": "same"}],
            review_state_by_url={"https://gitlab/x/-/merge_requests/3": ReviewState.APPROVED},
        )
        scanner = ReviewerPrsScanner(host=host)
        _mark_reviewed_helper(url="https://gitlab/x/-/merge_requests/3", sha="same")
        assert scanner.scan() == []

    def test_no_reviewer_returns_no_signals(self) -> None:
        host = FakeCodeHost(user="")
        scanner = ReviewerPrsScanner(host=host)
        assert scanner.scan() == []

    def test_pr_without_url_is_skipped(self) -> None:
        host = FakeCodeHost(
            user="alice",
            review_requested_prs=[{"sha": "abc"}],
        )
        scanner = ReviewerPrsScanner(host=host)
        assert scanner.scan() == []

    def test_head_sha_from_nested_head_dict(self) -> None:
        host = FakeCodeHost(
            user="alice",
            review_requested_prs=[
                {"html_url": "https://github.com/o/r/pull/1", "head": {"sha": "deadbeef"}},
            ],
        )
        scanner = ReviewerPrsScanner(host=host)
        signals = scanner.scan()
        assert signals[0].payload["head_sha"] == "deadbeef"

    def test_head_sha_from_diff_refs(self) -> None:
        host = FakeCodeHost(
            user="alice",
            review_requested_prs=[
                {"web_url": "https://gitlab/x/-/merge_requests/2", "diff_refs": {"head_sha": "feedface"}},
            ],
        )
        scanner = ReviewerPrsScanner(host=host)
        signals = scanner.scan()
        assert signals[0].payload["head_sha"] == "feedface"

    def test_head_sha_returns_empty_when_no_field_present(self) -> None:
        assert head_sha({}) == ""
        assert head_sha({"sha": 123}) == ""
        assert head_sha({"head": {"sha": 99}}) == ""
        assert head_sha({"diff_refs": {"head_sha": True}}) == ""

    def test_dismissed_after_approval_emits_approval_dismissed_signal(self) -> None:
        url = "https://gitlab/x/-/merge_requests/9"
        host = FakeCodeHost(
            user="alice",
            review_requested_prs=[{"web_url": url, "sha": "same"}],
            review_state_by_url={url: ReviewState.DISMISSED},
        )
        scanner = ReviewerPrsScanner(host=host)
        _mark_reviewed_helper(url=url, sha="same", state=ReviewState.APPROVED.value)
        signals = scanner.scan()
        assert [s.kind for s in signals] == ["reviewer_pr.approval_dismissed"]
        assert signals[0].payload["previous_state"] == ReviewState.APPROVED.value
        assert signals[0].payload["current_state"] == ReviewState.DISMISSED.value
        assert host.get_review_state_calls == [(url, "alice")]

    def test_pending_after_approval_emits_approval_dismissed_signal(self) -> None:
        """A re-request after dismissal surfaces as ``PENDING`` on the forge."""
        url = "https://github.com/o/r/pull/7"
        host = FakeCodeHost(
            user="alice",
            review_requested_prs=[{"html_url": url, "sha": "same"}],
            review_state_by_url={url: ReviewState.PENDING},
        )
        scanner = ReviewerPrsScanner(host=host)
        _mark_reviewed_helper(url=url, sha="same", state=ReviewState.APPROVED.value)
        signals = scanner.scan()
        assert [s.kind for s in signals] == ["reviewer_pr.approval_dismissed"]

    def test_state_unchanged_emits_no_signal_and_skips_state_fetch_when_sha_changes(self) -> None:
        """SHA-only path stays cheap — no per-PR review-state fetch when SHA differs."""
        url = "https://gitlab/x/-/merge_requests/3"
        host = FakeCodeHost(
            user="alice",
            review_requested_prs=[{"web_url": url, "sha": "newer"}],
            review_state_by_url={url: ReviewState.DISMISSED},
        )
        scanner = ReviewerPrsScanner(host=host)
        _mark_reviewed_helper(url=url, sha="older", state=ReviewState.APPROVED.value)
        signals = scanner.scan()
        assert [s.kind for s in signals] == ["reviewer_pr.new_sha"]
        assert host.get_review_state_calls == []

    def test_already_approved_still_approved_emits_nothing(self) -> None:
        url = "https://gitlab/x/-/merge_requests/3"
        host = FakeCodeHost(
            user="alice",
            review_requested_prs=[{"web_url": url, "sha": "same"}],
            review_state_by_url={url: ReviewState.APPROVED},
        )
        scanner = ReviewerPrsScanner(host=host)
        _mark_reviewed_helper(url=url, sha="same", state=ReviewState.APPROVED.value)
        assert scanner.scan() == []

    def test_changes_requested_after_approval_emits_nothing(self) -> None:
        """Author addressing changes is not the same as an approval being dismissed."""
        url = "https://gitlab/x/-/merge_requests/3"
        host = FakeCodeHost(
            user="alice",
            review_requested_prs=[{"web_url": url, "sha": "same"}],
            review_state_by_url={url: ReviewState.CHANGES_REQUESTED},
        )
        scanner = ReviewerPrsScanner(host=host)
        _mark_reviewed_helper(url=url, sha="same", state=ReviewState.APPROVED.value)
        assert scanner.scan() == []

    def test_mark_reviewed_persists_to_reviewer_ticket(self) -> None:
        """Recording a review creates/updates the reviewer-role ticket in the DB."""
        from teatree.loop.scanners.reviewer_prs import mark_reviewed  # noqa: PLC0415

        url = "https://gitlab/x/-/merge_requests/42"
        mark_reviewed(url=url, sha="abc")
        ticket = Ticket.objects.get(role=Ticket.Role.REVIEWER, issue_url=url)
        assert ticket.extra["reviewed_sha"] == "abc"
        assert ticket.extra["last_review_state"] == ReviewState.APPROVED.value

    def test_orphaned_pending_task_for_merged_mr_emits_orphaned_signal(self) -> None:
        """A PENDING reviewing task whose MR was merged externally is reaped (#998).

        Scenario: scanner sees MR X (open) on tick #1 → persistence creates
        Ticket(role=reviewer) + Task(phase=reviewing, status=PENDING). Before
        the slot processes the task, the MR is merged externally. On tick #2
        the API (state=opened) no longer returns the MR. Pre-fix, the PENDING
        task lingers forever and ``pending-spawn`` keeps surfacing it,
        dispatching a reviewer sub-agent every tick for nothing.

        Post-fix (#1074): absence from the scan is no longer sufficient —
        the scanner emits ``reviewer_pr.task_orphaned`` only after
        ``get_pr_open_state`` confirms the PR is genuinely MERGED/CLOSED.
        A mechanical handler then completes the task so ``pending-spawn``
        stops returning it.
        """
        from teatree.core.models import Session, Task  # noqa: PLC0415

        url = "https://gitlab/x/-/merge_requests/373"
        ticket = Ticket.objects.create(
            role=Ticket.Role.REVIEWER,
            issue_url=url,
            overlay="acme",
            extra={"reviewed_sha": "abc"},
        )
        session = Session.objects.create(ticket=ticket, agent_id="external-review")
        Task.objects.create(
            ticket=ticket,
            session=session,
            phase="reviewing",
            execution_target=Task.ExecutionTarget.HEADLESS,
            execution_reason="Review needed",
        )

        # API no longer returns the MR AND the forge confirms it merged.
        host = FakeCodeHost(
            user="alice",
            review_requested_prs=[],
            pr_open_state_by_url={url: PrOpenState.MERGED},
        )
        scanner = ReviewerPrsScanner(host=host)
        signals = scanner.scan()

        assert [s.kind for s in signals] == ["reviewer_pr.task_orphaned"]
        assert signals[0].payload["url"] == url
        assert signals[0].payload["ticket_id"] == ticket.pk

    def test_open_mr_absent_from_reviewer_scan_is_never_reaped(self) -> None:
        """#1074 regression: a live OPEN MR absent from the scan is NOT reaped.

        Slack-review-request MRs (slack.review_intent → schedule_external_review)
        never get a forge reviewer assignment, so ``list_review_requested_prs``
        never returns them — the URL is *permanently* absent from
        ``scanned_urls``. Pre-fix the orphan sweep reaped on absence alone:
        it emitted ``reviewer_pr.task_orphaned`` and the mechanical handler
        completed the reviewing Task, silently dropping a fully-OPEN review
        obligation and logging "MR no longer open" for an open MR.

        The surviving review obligation is the contract the bug violates:
        with the fix, NO orphan signal is emitted AND the PENDING reviewing
        Task is still PENDING (the obligation is preserved). Asserting the
        Task is still PENDING — not merely that no signal fired — is the
        anti-vacuity anchor: on the buggy code the signal fires, the
        mechanical handler runs, and the Task goes COMPLETED.
        """
        from teatree.core.models import Session, Task  # noqa: PLC0415

        url = "https://gitlab/x/-/merge_requests/1074"
        ticket = Ticket.objects.create(
            role=Ticket.Role.REVIEWER,
            issue_url=url,
            overlay="acme",
            extra={"reviewed_sha": "abc"},
        )
        session = Session.objects.create(ticket=ticket, agent_id="external-review")
        task = Task.objects.create(
            ticket=ticket,
            session=session,
            phase="reviewing",
            execution_target=Task.ExecutionTarget.HEADLESS,
            execution_reason="Review needed",
        )

        # No forge reviewer assignment (Slack-review-request MR) → absent
        # from the scan — but the MR is genuinely OPEN.
        host = FakeCodeHost(
            user="alice",
            review_requested_prs=[],
            pr_open_state_by_url={url: PrOpenState.OPEN},
        )
        scanner = ReviewerPrsScanner(host=host)
        signals = scanner.scan()

        assert "reviewer_pr.task_orphaned" not in [s.kind for s in signals]
        task.refresh_from_db()
        assert task.status == Task.Status.PENDING

    def test_unknown_pr_state_fails_open_and_is_not_reaped(self) -> None:
        """#1074: an UNKNOWN open-state (auth error, network, draft) fails open.

        ``get_pr_open_state`` returns UNKNOWN on any ambiguity. The sweep
        must NOT reap on UNKNOWN — fail open, never drop a review on doubt.
        """
        from teatree.core.models import Session, Task  # noqa: PLC0415

        url = "https://gitlab/x/-/merge_requests/1077"
        ticket = Ticket.objects.create(
            role=Ticket.Role.REVIEWER,
            issue_url=url,
            overlay="acme",
            extra={"reviewed_sha": "abc"},
        )
        session = Session.objects.create(ticket=ticket, agent_id="external-review")
        task = Task.objects.create(
            ticket=ticket,
            session=session,
            phase="reviewing",
            execution_target=Task.ExecutionTarget.HEADLESS,
            execution_reason="Review needed",
        )

        host = FakeCodeHost(
            user="alice",
            review_requested_prs=[],
            pr_open_state_by_url={url: PrOpenState.UNKNOWN},
        )
        scanner = ReviewerPrsScanner(host=host)
        signals = scanner.scan()

        assert "reviewer_pr.task_orphaned" not in [s.kind for s in signals]
        task.refresh_from_db()
        assert task.status == Task.Status.PENDING

    def test_orphaned_signal_not_emitted_when_mr_still_in_scan(self) -> None:
        """If the MR is still in the API response, no orphaning happens (#998)."""
        from teatree.core.models import Session, Task  # noqa: PLC0415

        url = "https://gitlab/x/-/merge_requests/374"
        ticket = Ticket.objects.create(
            role=Ticket.Role.REVIEWER,
            issue_url=url,
            overlay="acme",
            extra={"reviewed_sha": "abc"},
        )
        session = Session.objects.create(ticket=ticket, agent_id="external-review")
        Task.objects.create(
            ticket=ticket,
            session=session,
            phase="reviewing",
            execution_target=Task.ExecutionTarget.HEADLESS,
            execution_reason="Review needed",
        )

        # API still returns the MR with the same SHA → no orphan.
        host = FakeCodeHost(
            user="alice",
            review_requested_prs=[{"web_url": url, "sha": "abc"}],
        )
        scanner = ReviewerPrsScanner(host=host)
        signals = scanner.scan()

        # No orphan signal — the task is for a still-open MR.
        assert "reviewer_pr.task_orphaned" not in [s.kind for s in signals]

    def test_orphaned_signal_not_emitted_for_completed_task(self) -> None:
        """A COMPLETED reviewing task is not re-orphaned (#998)."""
        from teatree.core.models import Session, Task  # noqa: PLC0415

        url = "https://gitlab/x/-/merge_requests/375"
        ticket = Ticket.objects.create(
            role=Ticket.Role.REVIEWER,
            issue_url=url,
            overlay="acme",
            extra={"reviewed_sha": "abc", "last_review_state": ReviewState.APPROVED.value},
        )
        session = Session.objects.create(ticket=ticket, agent_id="external-review")
        Task.objects.create(
            ticket=ticket,
            session=session,
            phase="reviewing",
            execution_target=Task.ExecutionTarget.HEADLESS,
            execution_reason="Review needed",
            status=Task.Status.COMPLETED,
        )

        host = FakeCodeHost(user="alice", review_requested_prs=[])
        scanner = ReviewerPrsScanner(host=host)
        signals = scanner.scan()

        # No orphan signal — the task is already terminal.
        assert "reviewer_pr.task_orphaned" not in [s.kind for s in signals]

    def test_orphaned_signal_skipped_when_no_reviewer_resolvable(self) -> None:
        """No active reviewer identity → no scan, no orphan detection (#998).

        When ``current_user()`` returns empty and no explicit identities are
        configured, the scanner cannot tell whether the missing MR is
        genuinely closed/merged or simply unqueryable. Fail open: don't reap.
        """
        from teatree.core.models import Session, Task  # noqa: PLC0415

        url = "https://gitlab/x/-/merge_requests/376"
        ticket = Ticket.objects.create(
            role=Ticket.Role.REVIEWER,
            issue_url=url,
            overlay="acme",
            extra={"reviewed_sha": "abc"},
        )
        session = Session.objects.create(ticket=ticket, agent_id="external-review")
        Task.objects.create(
            ticket=ticket,
            session=session,
            phase="reviewing",
            execution_target=Task.ExecutionTarget.HEADLESS,
            execution_reason="Review needed",
        )

        host = FakeCodeHost(user="")  # no resolvable identity
        scanner = ReviewerPrsScanner(host=host)
        signals = scanner.scan()

        assert signals == []

    def test_orphan_sweep_scoped_to_scanner_overlay(self) -> None:
        """Orphan sweep must not cross overlay boundaries (#998 tightening).

        Two reviewer-role tickets on different overlays (e.g. GitHub vs.
        GitLab scanners) — running the scanner for one overlay must NOT
        emit an orphan signal for the other overlay's ticket, even though
        the other URL is absent from this scan's ``scanned_urls``.
        """
        from teatree.core.models import Session, Task  # noqa: PLC0415

        own_url = "https://github.com/o/r/pull/501"
        other_url = "https://gitlab/x/-/merge_requests/502"
        own_ticket = Ticket.objects.create(
            role=Ticket.Role.REVIEWER,
            issue_url=own_url,
            overlay="github-overlay",
            extra={"reviewed_sha": "abc"},
        )
        other_ticket = Ticket.objects.create(
            role=Ticket.Role.REVIEWER,
            issue_url=other_url,
            overlay="gitlab-overlay",
            extra={"reviewed_sha": "def"},
        )
        for ticket in (own_ticket, other_ticket):
            session = Session.objects.create(ticket=ticket, agent_id="external-review")
            Task.objects.create(
                ticket=ticket,
                session=session,
                phase="reviewing",
                execution_target=Task.ExecutionTarget.HEADLESS,
                execution_reason="Review needed",
            )

        # Run the scanner scoped to github-overlay only; API returns
        # nothing AND both PRs are confirmed merged on the forge.
        host = FakeCodeHost(
            user="alice",
            review_requested_prs=[],
            pr_open_state_by_url={own_url: PrOpenState.MERGED, other_url: PrOpenState.MERGED},
        )
        scanner = ReviewerPrsScanner(host=host, overlay_name="github-overlay")
        signals = scanner.scan()

        # Exactly one orphan signal — only the github-overlay ticket. The
        # gitlab-overlay ticket is invisible to this scanner pass.
        orphan_urls = [s.payload["url"] for s in signals if s.kind == "reviewer_pr.task_orphaned"]
        assert orphan_urls == [own_url]

    def test_orphan_sweep_unscoped_when_overlay_empty(self) -> None:
        """Empty overlay_name preserves the legacy unscoped sweep (#998).

        The single-overlay fallback path in tick.py builds the scanner
        without an overlay tag — for that path the previous unscoped
        behaviour is preserved (no overlay filter on the candidate query).
        """
        from teatree.core.models import Session, Task  # noqa: PLC0415

        urls = ["https://gitlab/x/-/merge_requests/601", "https://github.com/o/r/pull/602"]
        for idx, url in enumerate(urls):
            ticket = Ticket.objects.create(
                role=Ticket.Role.REVIEWER,
                issue_url=url,
                overlay=f"overlay-{idx}",
                extra={"reviewed_sha": "abc"},
            )
            session = Session.objects.create(ticket=ticket, agent_id="external-review")
            Task.objects.create(
                ticket=ticket,
                session=session,
                phase="reviewing",
                execution_target=Task.ExecutionTarget.HEADLESS,
                execution_reason="Review needed",
            )

        host = FakeCodeHost(
            user="alice",
            review_requested_prs=[],
            pr_open_state_by_url=dict.fromkeys(urls, PrOpenState.CLOSED),
        )
        scanner = ReviewerPrsScanner(host=host)  # overlay_name defaults to ""
        signals = scanner.scan()

        orphan_urls = sorted(s.payload["url"] for s in signals if s.kind == "reviewer_pr.task_orphaned")
        assert orphan_urls == sorted(urls)

    def test_orphan_sweep_no_op_when_ticket_model_unavailable(self) -> None:
        """``_orphaned_task_signals`` returns [] when Django isn't ready (#998 nit 2).

        Guards the defensive ``ticket_model is None`` branch — when the
        model registry can't be loaded (no Django setup, fresh subprocess),
        the sweep must skip silently rather than blow up.
        """
        from teatree.loop.scanners.reviewer_prs import _orphaned_task_signals  # noqa: PLC0415

        host = FakeCodeHost(user="alice")
        assert _orphaned_task_signals(None, set(), host) == []
        assert _orphaned_task_signals(None, {"https://x"}, host, "any-overlay") == []

    def test_legacy_json_cache_is_imported_then_deleted(self) -> None:
        """First scan migrates the legacy JSON file into reviewer tickets, then unlinks it."""
        import shutil  # noqa: PLC0415
        import tempfile  # noqa: PLC0415
        from unittest.mock import patch  # noqa: PLC0415

        import teatree.paths  # noqa: PLC0415

        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        (tmp / "loop").mkdir(parents=True)
        url_legacy = "https://gitlab/x/-/merge_requests/100"
        (tmp / "loop" / "reviewer_prs.json").write_text(
            f'{{"{url_legacy}": "legacy-sha"}}',
            encoding="utf-8",
        )

        host = FakeCodeHost(
            user="alice",
            review_requested_prs=[{"web_url": "https://gitlab/x/-/merge_requests/200", "sha": "abc"}],
        )
        with patch.object(teatree.paths, "DATA_DIR", tmp):
            ReviewerPrsScanner(host=host).scan()

        ticket = Ticket.objects.get(role=Ticket.Role.REVIEWER, issue_url=url_legacy)
        assert ticket.extra["reviewed_sha"] == "legacy-sha"
        assert not (tmp / "loop" / "reviewer_prs.json").exists()


class TestSlackMentionsScanner:
    def test_emits_one_signal_per_event(self, tmp_path: Path) -> None:
        backend = FakeMessaging(
            mentions=[{"ts": "1.0", "text": "hey"}],
            dms=[{"ts": "2.0", "text": "DM"}],
        )
        signals = SlackMentionsScanner(backend=backend, cursor_path=tmp_path / "cur.json").scan()
        kinds = sorted(s.kind for s in signals)
        assert kinds == ["slack.dm", "slack.mention"]

    def test_empty_when_no_events(self, tmp_path: Path) -> None:
        backend = FakeMessaging()
        assert SlackMentionsScanner(backend=backend, cursor_path=tmp_path / "cur.json").scan() == []

    def test_persists_cursor_after_emitting_events(self, tmp_path: Path) -> None:
        cursor = tmp_path / "cur.json"
        backend = FakeMessaging(
            mentions=[{"ts": "5.0", "text": "first"}, {"ts": "9.0", "text": "second"}],
        )
        SlackMentionsScanner(backend=backend, cursor_path=cursor).scan()
        assert "9.0" in cursor.read_text(encoding="utf-8")

    def test_event_ts_fallback_when_ts_absent(self, tmp_path: Path) -> None:
        backend = FakeMessaging(
            mentions=[{"event_ts": "11.0", "text": "without ts"}],
        )
        signals = SlackMentionsScanner(backend=backend, cursor_path=tmp_path / "cur.json").scan()
        assert signals[0].payload["ts"] == "11.0"

    def test_corrupt_cursor_file_treated_as_empty(self, tmp_path: Path) -> None:
        cursor = tmp_path / "cur.json"
        cursor.write_text("not-json", encoding="utf-8")
        backend = FakeMessaging(mentions=[{"ts": "1.0", "text": "x"}])
        signals = SlackMentionsScanner(backend=backend, cursor_path=cursor).scan()
        assert len(signals) == 1

    def test_cursor_with_non_string_values_filtered(self, tmp_path: Path) -> None:
        cursor = tmp_path / "cur.json"
        cursor.write_text(
            '{"mentions": "1.0", "dms": 99, "extra": null}',
            encoding="utf-8",
        )
        backend = FakeMessaging(mentions=[{"ts": "2.0", "text": "x"}])
        signals = SlackMentionsScanner(backend=backend, cursor_path=cursor).scan()
        assert len(signals) == 1

    def test_default_cursor_path_used_when_omitted(self) -> None:
        from teatree.loop.scanners.slack_mentions import _default_cursor_path  # noqa: PLC0415

        backend = FakeMessaging()
        scanner = SlackMentionsScanner(backend=backend)
        assert scanner.cursor_path == _default_cursor_path()


class TestNotionViewScanner:
    def test_no_op_when_client_missing(self) -> None:
        assert NotionViewScanner(client=None).scan() == []

    def test_emits_one_signal_per_unrouted_item(self) -> None:
        client = MagicMock()
        client.list_unrouted.return_value = [{"title": "Spec for API"}]
        signals = NotionViewScanner(client=client).scan()
        assert [s.kind for s in signals] == ["notion.unrouted"]


class TestPersistEntryLockedMergeExtra(TestCase):
    """#800 N3: ``_persist_entry`` routes through locked ``merge_extra``.

    It is the THIRD reviewed_sha/last_review_state co-writer; it now
    goes through the canonical locked ``Ticket.merge_extra`` and only
    when there is something to set (the new ``if set_keys:`` guard that
    replaced the old unconditional ``ticket.save``).
    """

    def test_persists_sha_and_state_via_merge_extra(self) -> None:
        _persist_entry(Ticket, "https://example.com/pr/1", CacheEntry(sha="abc", state="approved"))

        ticket = Ticket.objects.get(role="reviewer", issue_url="https://example.com/pr/1")
        assert ticket.extra == {"reviewed_sha": "abc", "last_review_state": "approved"}

    def test_empty_entry_writes_nothing(self) -> None:
        # entry with neither sha nor state → set_keys empty → no merge
        # call, no clobber (the #800-new `if set_keys:` False branch).
        _persist_entry(Ticket, "https://example.com/pr/2", CacheEntry(sha="", state=""))

        ticket = Ticket.objects.get(role="reviewer", issue_url="https://example.com/pr/2")
        assert ticket.extra in ({}, None)

    def test_does_not_clobber_concurrent_extra_writer(self) -> None:
        Ticket.objects.create(role="reviewer", issue_url="https://example.com/pr/3", extra={"pr_urls": ["u"]})
        _persist_entry(Ticket, "https://example.com/pr/3", CacheEntry(sha="def", state=""))

        ticket = Ticket.objects.get(role="reviewer", issue_url="https://example.com/pr/3")
        # The locked re-read merged reviewed_sha WITHOUT dropping pr_urls.
        assert ticket.extra == {"pr_urls": ["u"], "reviewed_sha": "def"}
