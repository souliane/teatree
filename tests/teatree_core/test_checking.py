"""DB-backed tests for the read-only checking gatherer + renderer (#1529).

Mirrors ``test_standup.py``: real rows in a tmp DB, the clock injected via
``now=``/``since=``, no network/git/API. ``merged_at``/``created_at``/
``ended_at`` are backdated with ``update()`` to place rows inside or outside
the half-open window ``[since, now)``.

The load-bearing renderer assertions: every PR/ticket reference is a markdown
link (NO bare numeric ids), groups cap at 5 with "…and X more", empty groups
are omitted, and an all-empty report collapses to one line.
"""

import json
import re
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from teatree.core._checking_gather import pr_url_for
from teatree.core.checking import (
    AllOverlaysReport,
    CheckGroup,
    CheckingReport,
    CheckItem,
    build_pr_url,
    gather_all_overlays_report,
    gather_checking_report,
)
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.core.models.merge_clear import ClearRequest, MergeAudit, MergeClear
from teatree.core.models.session import Session
from teatree.core.models.task import Task, TaskAttempt
from teatree.core.models.ticket import Ticket
from teatree.core.models.transition import TicketTransition

_SHA = "a" * 40
_REVIEWER = "cold-reviewer"


class CheckingTestBase(TestCase):
    OVERLAY = "acme"

    def setUp(self) -> None:
        self.now = timezone.now()
        self.since = self.now - timedelta(hours=24)

    def _ticket(self, *, number: int = 42, state: str = Ticket.State.IN_REVIEW, **extra: object) -> Ticket:
        ticket = Ticket.objects.create(
            overlay=self.OVERLAY,
            issue_url=f"https://github.com/acme/widgets/issues/{number}",
            state=state,
            short_description=f"ticket {number} work",
        )
        if extra:
            ticket.extra = dict(extra)
            ticket.save(update_fields=["extra"])
        return ticket

    def _merge(self, ticket: Ticket | None, *, pr_id: int, slug: str, hours_ago: float) -> MergeAudit:
        clear = MergeClear.issue(
            ClearRequest(
                pr_id=pr_id,
                slug=slug,
                reviewed_sha=_SHA,
                reviewer_identity=_REVIEWER,
                ticket=ticket,
            ),
        )
        audit = MergeAudit.objects.create(clear=clear, merged_sha=_SHA, required_checks_status="success")
        MergeAudit.objects.filter(pk=audit.pk).update(merged_at=self.now - timedelta(hours=hours_ago))
        return audit

    def _transition(self, ticket: Ticket, *, frm: str, to: str, hours_ago: float) -> None:
        tr = TicketTransition.objects.create(ticket=ticket, from_state=frm, to_state=to)
        TicketTransition.objects.filter(pk=tr.pk).update(created_at=self.now - timedelta(hours=hours_ago))

    def _attempt(self, ticket: Ticket, *, hours_ago: float, exit_code: int = 0) -> None:
        session = Session.objects.create(ticket=ticket, agent_id="a")
        task = Task.objects.create(ticket=ticket, session=session, phase="coding")
        attempt = TaskAttempt.objects.create(
            task=task,
            execution_target=Task.ExecutionTarget.HEADLESS,
            exit_code=exit_code,
        )
        TaskAttempt.objects.filter(pk=attempt.pk).update(ended_at=self.now - timedelta(hours=hours_ago))


class TestMergedGroup(CheckingTestBase):
    def test_merge_in_window_is_reported(self) -> None:
        ticket = self._ticket()
        self._merge(ticket, pr_id=7, slug="acme/widgets", hours_ago=3)
        report = gather_checking_report(since=self.since, now=self.now, overlay_name=self.OVERLAY)
        assert report.merged.total == 1
        item = report.merged.items[0]
        assert item.label == "acme/widgets#7"
        assert item.detail == "ticket 42 work"

    def test_merge_outside_window_excluded(self) -> None:
        ticket = self._ticket()
        self._merge(ticket, pr_id=7, slug="acme/widgets", hours_ago=48)
        report = gather_checking_report(since=self.since, now=self.now, overlay_name=self.OVERLAY)
        assert report.merged.total == 0

    def test_overlay_filter_excludes_other_overlay(self) -> None:
        mine = self._ticket(number=1)
        self._merge(mine, pr_id=10, slug="acme/widgets", hours_ago=2)
        other = Ticket.objects.create(
            overlay="other",
            issue_url="https://github.com/other/x/issues/2",
            state=Ticket.State.IN_REVIEW,
        )
        self._merge(other, pr_id=11, slug="other/x", hours_ago=2)
        report = gather_checking_report(since=self.since, now=self.now, overlay_name=self.OVERLAY)
        assert [item.label for item in report.merged.items] == ["acme/widgets#10"]

    def test_url_prefers_stored_pr_url(self) -> None:
        ticket = self._ticket(pr_urls=["https://github.com/acme/widgets/pull/7"])
        self._merge(ticket, pr_id=7, slug="acme/widgets", hours_ago=2)
        report = gather_checking_report(since=self.since, now=self.now, overlay_name=self.OVERLAY)
        assert report.merged.items[0].url == "https://github.com/acme/widgets/pull/7"

    def test_url_falls_back_to_host_aware_builder(self) -> None:
        ticket = self._ticket()
        self._merge(ticket, pr_id=7, slug="acme/widgets", hours_ago=2)
        report = gather_checking_report(since=self.since, now=self.now, overlay_name=self.OVERLAY, code_host="github")
        assert report.merged.items[0].url == "https://github.com/acme/widgets/pull/7"

    def test_gitlab_builder_uses_merge_requests_path(self) -> None:
        ticket = self._ticket()
        self._merge(ticket, pr_id=9, slug="acme/widgets", hours_ago=2)
        report = gather_checking_report(since=self.since, now=self.now, overlay_name=self.OVERLAY, code_host="gitlab")
        assert report.merged.items[0].url == "https://gitlab.com/acme/widgets/-/merge_requests/9"

    def test_unresolvable_repo_falls_back_to_issue_url(self) -> None:
        # No owner/repo-shaped slug, a ticket whose issue_url is not a
        # recognisable forge repo, and no clone-origin remote: no real repo
        # resolves, so the reference falls back to the ticket's issue URL
        # (still clickable) rather than a wrong-host workstream link (#1559).
        ticket = Ticket.objects.create(
            overlay=self.OVERLAY,
            issue_url="https://example.invalid/not-an-issue",
            state=Ticket.State.IN_REVIEW,
            short_description="ticket work",
        )
        self._merge(ticket, pr_id=7, slug="some-workstream-name", hours_ago=2)
        with patch("teatree.core.merge.pr_slug_resolution._project_repo_slug", return_value=""):
            report = gather_checking_report(since=self.since, now=self.now, overlay_name=self.OVERLAY)
        assert report.merged.items[0].url == ticket.issue_url

    def test_workstream_slug_never_builds_wrong_host_url(self) -> None:
        # A ticket-bearing CLEAR whose slug is a workstream slug must resolve
        # the ticket's real repo, never emit ``github.com/<workstream>/...``
        # (#1559 bug 2).
        ticket = self._ticket()
        self._merge(ticket, pr_id=7, slug="statusline-stale-wakeup", hours_ago=2)
        report = gather_checking_report(since=self.since, now=self.now, overlay_name=self.OVERLAY, code_host="github")
        item = report.merged.items[0]
        assert item.url == "https://github.com/acme/widgets/pull/7"
        assert "statusline-stale-wakeup" not in item.url
        assert item.label == "acme/widgets#7"


class TestMergedGroupNullTicketRepoScope(CheckingTestBase):
    """A NULL-ticket ceremony merge is scoped to the overlay by its repo (#1559 bug 1).

    The ceremony ``ticket clear`` is issued without ``--ticket-id``, so
    ``MergeClear.ticket`` is NULL for nearly every CLEAR. A ticket-FK JOIN
    silently drops those, leaving the merged group almost always empty. The
    read-side scope must instead match the CLEAR's RESOLVED repo against the
    overlay's repo set — without over-reporting a different overlay's merges.
    """

    def _null_ticket_merge(self, *, pr_id: int, slug: str, hours_ago: float) -> MergeAudit:
        clear = MergeClear.objects.create(
            ticket=None,
            pr_id=pr_id,
            slug=slug,
            reviewed_sha=_SHA,
            reviewer_identity=_REVIEWER,
            gh_verify_result=MergeClear.VerifyResult.GREEN,
            blast_class=MergeClear.BlastClass.LOGIC,
        )
        audit = MergeAudit.objects.create(clear=clear, merged_sha=_SHA, required_checks_status="success")
        MergeAudit.objects.filter(pk=audit.pk).update(merged_at=self.now - timedelta(hours=hours_ago))
        return audit

    def test_null_ticket_merge_in_overlay_repo_appears(self) -> None:
        # owner/repo-shaped slug resolves to itself; the repo belongs to the
        # overlay, so the NULL-ticket merge surfaces.
        self._null_ticket_merge(pr_id=12, slug="acme/widgets", hours_ago=2)
        report = gather_checking_report(
            since=self.since, now=self.now, overlay_name=self.OVERLAY, overlay_repos=["acme/widgets"]
        )
        assert [item.label for item in report.merged.items] == ["acme/widgets#12"]

    def test_null_ticket_merge_in_overlay_matched_by_bare_repo_name(self) -> None:
        # An overlay that declares a bare ``repo`` name (no owner) still scopes
        # a resolved ``owner/repo`` whose repo segment matches.
        self._null_ticket_merge(pr_id=13, slug="acme/widgets", hours_ago=2)
        report = gather_checking_report(
            since=self.since, now=self.now, overlay_name=self.OVERLAY, overlay_repos=["widgets"]
        )
        assert [item.label for item in report.merged.items] == ["acme/widgets#13"]

    def test_null_ticket_merge_in_other_overlay_repo_excluded(self) -> None:
        # A NULL-ticket merge whose resolved repo is NOT in this overlay's repo
        # set must NOT appear — no blanket ``ticket IS NULL`` over-reporting.
        self._null_ticket_merge(pr_id=14, slug="other-org/other-repo", hours_ago=2)
        report = gather_checking_report(
            since=self.since, now=self.now, overlay_name=self.OVERLAY, overlay_repos=["acme/widgets"]
        )
        assert report.merged.total == 0

    def test_null_ticket_and_ticket_bearing_merges_both_scoped(self) -> None:
        # The ticket-bearing CLEAR keeps its precise overlay link; the
        # NULL-ticket CLEAR is scoped by repo. Both land; a foreign one drops.
        ticket = self._ticket(number=5)
        self._merge(ticket, pr_id=20, slug="acme/widgets", hours_ago=3)
        self._null_ticket_merge(pr_id=21, slug="acme/widgets", hours_ago=2)
        self._null_ticket_merge(pr_id=22, slug="other-org/other-repo", hours_ago=1)
        report = gather_checking_report(
            since=self.since, now=self.now, overlay_name=self.OVERLAY, overlay_repos=["acme/widgets"]
        )
        labels = {item.label for item in report.merged.items}
        assert labels == {"acme/widgets#20", "acme/widgets#21"}


class TestInFlightGroup(CheckingTestBase):
    def test_latest_transition_per_ticket(self) -> None:
        ticket = self._ticket(state=Ticket.State.CODED)
        self._transition(ticket, frm=Ticket.State.SCOPED, to=Ticket.State.STARTED, hours_ago=6)
        self._transition(ticket, frm=Ticket.State.STARTED, to=Ticket.State.CODED, hours_ago=1)
        report = gather_checking_report(since=self.since, now=self.now, overlay_name=self.OVERLAY)
        assert report.in_flight.total == 1
        item = report.in_flight.items[0]
        assert item.detail == "→ coded"
        assert item.label == "#42"

    def test_issue_url_is_used(self) -> None:
        ticket = self._ticket()
        self._transition(ticket, frm=Ticket.State.STARTED, to=Ticket.State.CODED, hours_ago=1)
        report = gather_checking_report(since=self.since, now=self.now, overlay_name=self.OVERLAY)
        assert report.in_flight.items[0].url == ticket.issue_url

    def test_transition_outside_window_excluded(self) -> None:
        ticket = self._ticket()
        self._transition(ticket, frm=Ticket.State.STARTED, to=Ticket.State.CODED, hours_ago=48)
        report = gather_checking_report(since=self.since, now=self.now, overlay_name=self.OVERLAY)
        assert report.in_flight.total == 0

    def test_no_issue_url_falls_back_to_stored_pr_url(self) -> None:
        # A ticket with no issue URL but a stored PR URL surfaces the PR URL,
        # never a bare id.
        ticket = Ticket.objects.create(
            overlay=self.OVERLAY,
            issue_url="",
            state=Ticket.State.IN_REVIEW,
            extra={"pr_urls": ["https://github.com/acme/widgets/pull/55"]},
        )
        self._transition(ticket, frm=Ticket.State.SHIPPED, to=Ticket.State.IN_REVIEW, hours_ago=1)
        report = gather_checking_report(since=self.since, now=self.now, overlay_name=self.OVERLAY)
        assert report.in_flight.items[0].url == "https://github.com/acme/widgets/pull/55"


class TestNeedsYouGroup(CheckingTestBase):
    def test_pending_question_pre_window_still_shown(self) -> None:
        question = DeferredQuestion.record("Should I ship the widget?")
        # Backdate well before the window — a pending question is not
        # window-bounded, so it must still surface.
        DeferredQuestion.objects.filter(pk=question.pk).update(
            created_at=self.now - timedelta(days=10),
        )
        report = gather_checking_report(since=self.since, now=self.now, overlay_name=self.OVERLAY)
        assert report.needs_you.total == 1
        item = report.needs_you.items[0]
        # Local handle, never a bare ``#NNN`` (that reads like an unlinked
        # forge issue ref and breaks the all-refs-clickable contract).
        assert item.label.startswith(f"Q{question.pk}:")
        assert "#" not in item.label
        assert f"questions answer {question.pk}" in item.detail

    def test_failed_attempt_surfaces_blocker_with_clickable_url(self) -> None:
        ticket = self._ticket(state=Ticket.State.STARTED)
        self._attempt(ticket, hours_ago=2, exit_code=1)
        report = gather_checking_report(since=self.since, now=self.now, overlay_name=self.OVERLAY)
        assert report.needs_you.total == 1
        item = report.needs_you.items[0]
        assert item.label == "#42"
        assert item.url == ticket.issue_url
        assert item.detail == "failed agent run"

    def test_successful_attempt_does_not_block(self) -> None:
        ticket = self._ticket(state=Ticket.State.STARTED)
        self._attempt(ticket, hours_ago=2, exit_code=0)
        report = gather_checking_report(since=self.since, now=self.now, overlay_name=self.OVERLAY)
        assert report.needs_you.total == 0

    def test_failed_attempts_dedup_per_ticket_and_cap(self) -> None:
        # Six distinct blocked tickets exceed the cap; the group reports the
        # full total and renders only the cap.
        for n in range(6):
            blocked = self._ticket(number=300 + n, state=Ticket.State.STARTED)
            self._attempt(blocked, hours_ago=1, exit_code=1)
        # A second failed run on one ticket must not double-count it.
        self._attempt(Ticket.objects.get(issue_url__endswith="/300"), hours_ago=0.5, exit_code=1)
        report = gather_checking_report(since=self.since, now=self.now, overlay_name=self.OVERLAY)
        assert report.needs_you.total == 6
        assert len(report.needs_you.items) == 5


class TestTerseFormatting(CheckingTestBase):
    def test_all_empty_collapses_to_single_line(self) -> None:
        report = gather_checking_report(since=self.since, now=self.now, overlay_name=self.OVERLAY)
        terse = report.to_terse(overlay_name=self.OVERLAY)
        assert terse.startswith("Nothing since ")
        assert "\n" not in terse

    def test_empty_group_is_omitted(self) -> None:
        ticket = self._ticket()
        self._merge(ticket, pr_id=7, slug="acme/widgets", hours_ago=2)
        report = gather_checking_report(since=self.since, now=self.now, overlay_name=self.OVERLAY)
        terse = report.to_terse(overlay_name=self.OVERLAY)
        assert "Merged" in terse
        assert "In-flight" not in terse
        assert "Needs you" not in terse

    def test_header_has_overlay_and_no_preamble(self) -> None:
        ticket = self._ticket()
        self._merge(ticket, pr_id=7, slug="acme/widgets", hours_ago=2)
        report = gather_checking_report(since=self.since, now=self.now, overlay_name=self.OVERLAY)
        terse = report.to_terse(overlay_name=self.OVERLAY)
        first = terse.splitlines()[0]
        assert first.startswith("Since ")
        assert first.endswith(f"· {self.OVERLAY}")

    def test_cap_appends_and_x_more(self) -> None:
        for n in range(7):
            ticket = self._ticket(number=100 + n)
            self._merge(ticket, pr_id=200 + n, slug="acme/widgets", hours_ago=1)
        report = gather_checking_report(since=self.since, now=self.now, overlay_name=self.OVERLAY)
        assert report.merged.total == 7
        terse = report.to_terse(overlay_name=self.OVERLAY)
        assert "…and 2 more" in terse
        # Exactly the cap of items rendered as links.
        assert terse.count("](") == 5

    def test_no_bare_numeric_ids_in_reference_lines(self) -> None:
        # Exercise ALL three groups, including a pending DeferredQuestion —
        # the question path sets url="" and previously rendered a bare ``Q#N``,
        # which this test must now catch.
        merged_ticket = self._ticket(number=42)
        self._merge(merged_ticket, pr_id=7, slug="acme/widgets", hours_ago=2)
        inflight_ticket = self._ticket(number=43, state=Ticket.State.CODED)
        self._transition(inflight_ticket, frm=Ticket.State.STARTED, to=Ticket.State.CODED, hours_ago=1)
        DeferredQuestion.record("Should I ship the widget rename?")
        report = gather_checking_report(since=self.since, now=self.now, overlay_name=self.OVERLAY, code_host="github")
        terse = report.to_terse(overlay_name=self.OVERLAY)
        for line in terse.splitlines():
            stripped = line.strip()
            if not stripped.startswith("- "):
                continue
            # A reference line either carries a clickable markdown link
            # (PR/issue/ticket) or is a question line whose content is an
            # actionable CLI command. In BOTH cases, no bare ``#NNN`` token may
            # appear OUTSIDE a [..](..) link — that is the contract violation.
            outside_links = re.sub(r"\[[^\]]*\]\([^)]*\)", "", stripped)
            assert not re.search(r"#\d", outside_links), f"bare #-id outside a link: {line!r}"
            has_link = bool(re.search(r"\]\(https?://", stripped))
            is_command = "t3 " in stripped and "questions answer" in stripped
            assert has_link or is_command, f"reference line is neither a link nor a command: {line!r}"


class TestJsonShape(CheckingTestBase):
    def test_to_dict_is_json_serializable_and_stable(self) -> None:
        ticket = self._ticket()
        self._merge(ticket, pr_id=7, slug="acme/widgets", hours_ago=2)
        report = gather_checking_report(since=self.since, now=self.now, overlay_name=self.OVERLAY)
        payload = report.to_dict()
        encoded = json.dumps(payload)  # must not raise
        decoded = json.loads(encoded)
        assert set(decoded) == {"since", "merged", "in_flight", "needs_you", "terse"}
        assert decoded["merged"]["total"] == 1
        assert decoded["merged"]["items"][0]["label"] == "acme/widgets#7"


class TestPureRenderers:
    def test_build_pr_url_blank_slug_is_empty(self) -> None:
        assert build_pr_url(slug="", pr_id=1, code_host="github") == ""

    def test_check_item_renders_clickable_link(self) -> None:
        item = CheckItem(label="acme/x#3", url="https://github.com/acme/x/pull/3", detail="fix")
        assert item.render() == "  - [acme/x#3](https://github.com/acme/x/pull/3) — fix"

    def test_check_group_empty_renders_nothing(self) -> None:
        assert CheckGroup(title="Merged", items=[], total=0).render() == []

    def test_check_group_renders_more_line(self) -> None:
        items = [CheckItem(label=f"acme/x#{n}", url=f"https://h/{n}") for n in range(5)]
        group = CheckGroup(title="Merged", items=items, total=8)
        rendered = group.render(cap=5)
        assert rendered[0] == "Merged"
        assert rendered[-1] == "  …and 3 more"

    def test_report_naive_since_renders_without_error(self) -> None:
        report = CheckingReport(
            since=datetime(2026, 5, 30, 9, 0, tzinfo=UTC),
            merged=CheckGroup(title="Merged"),
            in_flight=CheckGroup(title="In-flight"),
            needs_you=CheckGroup(title="Needs you"),
        )
        assert report.to_terse().startswith("Nothing since ")


class TestPrUrlForPathSegmentMatch(TestCase):
    """``pr_url_for`` must match pr_id as a path segment, not a bare substring (#1621).

    When ``pr_urls`` contains both ``/pull/123`` and ``/pull/12``, resolving
    pr_id=12 must return the ``/pull/12`` URL, not ``/pull/123``.
    """

    def _ticket_with_urls(self, *urls: str) -> Ticket:
        t = Ticket.objects.create(
            overlay="acme",
            issue_url="https://github.com/synthetic-owner/synthetic-repo/issues/1",
            state=Ticket.State.IN_REVIEW,
        )
        t.extra = {"pr_urls": list(urls)}
        t.save(update_fields=["extra"])
        return t

    def test_github_shorter_id_not_confused_with_longer(self) -> None:
        ticket = self._ticket_with_urls(
            "https://github.com/synthetic-owner/synthetic-repo/pull/123",
            "https://github.com/synthetic-owner/synthetic-repo/pull/12",
        )
        result = pr_url_for(ticket, repo_slug="synthetic-owner/synthetic-repo", pr_id=12, code_host="github")
        assert result == "https://github.com/synthetic-owner/synthetic-repo/pull/12"

    def test_gitlab_shorter_id_not_confused_with_longer(self) -> None:
        ticket = self._ticket_with_urls(
            "https://gitlab.com/synthetic-owner/synthetic-repo/-/merge_requests/123",
            "https://gitlab.com/synthetic-owner/synthetic-repo/-/merge_requests/12",
        )
        result = pr_url_for(ticket, repo_slug="synthetic-owner/synthetic-repo", pr_id=12, code_host="gitlab")
        assert result == "https://gitlab.com/synthetic-owner/synthetic-repo/-/merge_requests/12"

    def test_no_match_falls_through_to_builder(self) -> None:
        ticket = self._ticket_with_urls(
            "https://github.com/synthetic-owner/synthetic-repo/pull/99",
        )
        result = pr_url_for(ticket, repo_slug="synthetic-owner/synthetic-repo", pr_id=7, code_host="github")
        assert result == "https://github.com/synthetic-owner/synthetic-repo/pull/7"

    def test_single_entry_exact_match_still_works(self) -> None:
        ticket = self._ticket_with_urls(
            "https://github.com/synthetic-owner/synthetic-repo/pull/7",
        )
        result = pr_url_for(ticket, repo_slug="synthetic-owner/synthetic-repo", pr_id=7, code_host="github")
        assert result == "https://github.com/synthetic-owner/synthetic-repo/pull/7"


class TestAllOverlaysAggregation(CheckingTestBase):
    """``gather_all_overlays_report`` merges rows from multiple overlays (#1529)."""

    OVERLAY_B = "beta"

    def _ticket_b(self, *, number: int = 99, state: str = Ticket.State.IN_REVIEW) -> Ticket:
        return Ticket.objects.create(
            overlay=self.OVERLAY_B,
            issue_url=f"https://github.com/beta/core/issues/{number}",
            state=state,
            short_description=f"ticket {number} beta work",
        )

    def _merge_b(self, ticket: Ticket, *, pr_id: int, hours_ago: float) -> MergeAudit:
        clear = MergeClear.issue(
            ClearRequest(
                pr_id=pr_id,
                slug="beta/core",
                reviewed_sha=_SHA,
                reviewer_identity=_REVIEWER,
                ticket=ticket,
            ),
        )
        audit = MergeAudit.objects.create(clear=clear, merged_sha=_SHA, required_checks_status="success")
        MergeAudit.objects.filter(pk=audit.pk).update(merged_at=self.now - timedelta(hours=hours_ago))
        return audit

    def test_all_overlays_aggregation(self) -> None:
        ticket_a = self._ticket(number=1)
        self._merge(ticket_a, pr_id=10, slug="acme/widgets", hours_ago=2)
        ticket_b = self._ticket_b(number=2)
        self._merge_b(ticket_b, pr_id=20, hours_ago=2)

        overlay_windows = {
            self.OVERLAY: (self.since, self.now),
            self.OVERLAY_B: (self.since, self.now),
        }
        overlay_configs = {
            self.OVERLAY: ("github", ["acme/widgets"]),
            self.OVERLAY_B: ("github", ["beta/core"]),
        }
        report = gather_all_overlays_report(overlay_windows=overlay_windows, overlay_configs=overlay_configs)

        labels = {item.label for item in report.merged.items}
        assert "acme/widgets#10" in labels
        assert "beta/core#20" in labels

    def test_inline_overlay_tag_in_merged_items(self) -> None:
        ticket_a = self._ticket(number=1)
        self._merge(ticket_a, pr_id=10, slug="acme/widgets", hours_ago=2)
        ticket_b = self._ticket_b(number=2)
        self._merge_b(ticket_b, pr_id=20, hours_ago=2)

        overlay_windows = {
            self.OVERLAY: (self.since, self.now),
            self.OVERLAY_B: (self.since, self.now),
        }
        overlay_configs = {
            self.OVERLAY: ("github", ["acme/widgets"]),
            self.OVERLAY_B: ("github", ["beta/core"]),
        }
        report = gather_all_overlays_report(overlay_windows=overlay_windows, overlay_configs=overlay_configs)
        terse = report.to_terse()

        assert f"[{self.OVERLAY}]" in terse
        assert f"[{self.OVERLAY_B}]" in terse

    def test_empty_across_all_overlays(self) -> None:
        overlay_windows = {
            self.OVERLAY: (self.since, self.now),
            self.OVERLAY_B: (self.since, self.now),
        }
        overlay_configs = {
            self.OVERLAY: ("github", []),
            self.OVERLAY_B: ("github", []),
        }
        report = gather_all_overlays_report(overlay_windows=overlay_windows, overlay_configs=overlay_configs)
        terse = report.to_terse()

        assert terse.startswith("Nothing since ")
        assert "\n" not in terse

    def test_deferred_questions_not_duplicated_in_multi_overlay(self) -> None:
        DeferredQuestion.record("Should I proceed?")

        overlay_windows = {
            self.OVERLAY: (self.since, self.now),
            self.OVERLAY_B: (self.since, self.now),
        }
        overlay_configs = {
            self.OVERLAY: ("github", []),
            self.OVERLAY_B: ("github", []),
        }
        report = gather_all_overlays_report(overlay_windows=overlay_windows, overlay_configs=overlay_configs)

        question_items = [item for item in report.needs_you.items if item.label.startswith("Q")]
        assert len(question_items) == 1

    def test_all_overlays_report_is_dataclass(self) -> None:
        overlay_windows = {self.OVERLAY: (self.since, self.now)}
        overlay_configs = {self.OVERLAY: ("github", [])}
        report = gather_all_overlays_report(overlay_windows=overlay_windows, overlay_configs=overlay_configs)
        assert isinstance(report, AllOverlaysReport)
