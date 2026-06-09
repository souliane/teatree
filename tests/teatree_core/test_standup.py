"""DB-backed tests for the read-only standup generator (issue #563).

The generator cross-references ``TicketTransition`` and ``TaskAttempt``
over a time window and emits a structured, read-only report. ``git log``
is the only external touch and is injected so the test stays hermetic.
``created_at``/``started_at`` are ``auto_now_add``; backdate with
``update()`` to place rows inside / outside the window.
"""

import tempfile
from datetime import timedelta
from pathlib import Path
from typing import cast

from django.test import TestCase
from django.utils import timezone

from teatree.core.models.session import Session
from teatree.core.models.task import Task, TaskAttempt
from teatree.core.models.ticket import Ticket
from teatree.core.models.transition import TicketTransition
from teatree.core.models.worktree import Worktree
from teatree.core.standup import StandupBlocker, StandupLine, StandupReport, generate_standup


class GenerateStandupTests(TestCase):
    OVERLAY = "acme"

    def setUp(self) -> None:
        self.since = timezone.now() - timedelta(days=1)

    def _ticket(self, *, state: str = Ticket.State.CODED, number: int = 42) -> Ticket:
        return Ticket.objects.create(
            overlay=self.OVERLAY,
            issue_url=f"https://example.com/issues/{number}",
            state=state,
        )

    def _transition(self, ticket: Ticket, *, frm: str, to: str, hours_ago: float) -> None:
        tr = TicketTransition.objects.create(ticket=ticket, from_state=frm, to_state=to)
        TicketTransition.objects.filter(pk=tr.pk).update(
            created_at=timezone.now() - timedelta(hours=hours_ago),
        )

    def _attempt(self, ticket: Ticket, *, hours_ago: float, exit_code: int = 0) -> None:
        session = Session.objects.create(ticket=ticket, agent_id="a")
        task = Task.objects.create(ticket=ticket, session=session, phase="coding")
        att = TaskAttempt.objects.create(
            task=task,
            execution_target=Task.ExecutionTarget.HEADLESS,
            ended_at=timezone.now() - timedelta(hours=hours_ago),
            exit_code=exit_code,
        )
        TaskAttempt.objects.filter(pk=att.pk).update(
            started_at=timezone.now() - timedelta(hours=hours_ago),
        )

    def test_returns_report_dataclass(self) -> None:
        report = generate_standup(since=self.since, overlay_name=self.OVERLAY)
        assert isinstance(report, StandupReport)
        assert report.yesterday == []
        assert report.blockers == []

    def test_transition_within_window_is_reported(self) -> None:
        ticket = self._ticket()
        self._transition(ticket, frm=Ticket.State.STARTED, to=Ticket.State.CODED, hours_ago=3)
        report = generate_standup(since=self.since, overlay_name=self.OVERLAY)
        assert len(report.yesterday) == 1
        line = report.yesterday[0]
        assert line.ticket_number == "42"
        assert line.from_state == Ticket.State.STARTED
        assert line.to_state == Ticket.State.CODED

    def test_multiple_transitions_collapse_to_latest(self) -> None:
        ticket = self._ticket()
        self._transition(ticket, frm=Ticket.State.SCOPED, to=Ticket.State.STARTED, hours_ago=6)
        self._transition(ticket, frm=Ticket.State.STARTED, to=Ticket.State.CODED, hours_ago=1)
        report = generate_standup(since=self.since, overlay_name=self.OVERLAY)
        assert len(report.yesterday) == 1
        assert report.yesterday[0].from_state == Ticket.State.STARTED
        assert report.yesterday[0].to_state == Ticket.State.CODED

    def test_transition_outside_window_excluded(self) -> None:
        ticket = self._ticket()
        self._transition(ticket, frm=Ticket.State.STARTED, to=Ticket.State.CODED, hours_ago=48)
        report = generate_standup(since=self.since, overlay_name=self.OVERLAY)
        assert report.yesterday == []

    def test_attempt_counts_aggregated_per_ticket(self) -> None:
        ticket = self._ticket()
        self._transition(ticket, frm=Ticket.State.STARTED, to=Ticket.State.CODED, hours_ago=3)
        self._attempt(ticket, hours_ago=2)
        self._attempt(ticket, hours_ago=1)
        report = generate_standup(since=self.since, overlay_name=self.OVERLAY)
        assert report.yesterday[0].attempt_count == 2

    def test_failed_attempt_surfaces_blocker(self) -> None:
        ticket = self._ticket(state=Ticket.State.STARTED)
        self._attempt(ticket, hours_ago=2, exit_code=1)
        report = generate_standup(since=self.since, overlay_name=self.OVERLAY)
        assert len(report.blockers) == 1
        assert report.blockers[0].ticket_number == "42"

    def test_overlay_isolation(self) -> None:
        mine = self._ticket(number=1)
        self._transition(mine, frm=Ticket.State.STARTED, to=Ticket.State.CODED, hours_ago=2)
        other = Ticket.objects.create(
            overlay="other",
            issue_url="https://example.com/issues/2",
            state=Ticket.State.CODED,
        )
        self._transition(other, frm=Ticket.State.STARTED, to=Ticket.State.CODED, hours_ago=2)
        report = generate_standup(since=self.since, overlay_name=self.OVERLAY)
        assert [line.ticket_number for line in report.yesterday] == ["1"]

    def test_git_log_collector_is_injected_and_optional(self) -> None:
        ticket = self._ticket()
        self._transition(ticket, frm=Ticket.State.STARTED, to=Ticket.State.CODED, hours_ago=2)

        def fake_commits(_ticket: Ticket) -> list[str]:
            return ["abc123 fix the thing"]

        report = generate_standup(
            since=self.since,
            overlay_name=self.OVERLAY,
            commit_collector=fake_commits,
        )
        assert report.yesterday[0].commits == ["abc123 fix the thing"]

    def test_render_markdown_is_pure_string(self) -> None:
        ticket = self._ticket()
        self._transition(ticket, frm=Ticket.State.STARTED, to=Ticket.State.CODED, hours_ago=2)
        report = generate_standup(since=self.since, overlay_name=self.OVERLAY)
        md = report.to_markdown()
        assert md.startswith("## Yesterday")
        assert "TICKET-42" in md
        assert "## Blockers" in md

    def test_to_dict_is_json_safe(self) -> None:
        ticket = self._ticket()
        self._transition(ticket, frm=Ticket.State.STARTED, to=Ticket.State.CODED, hours_ago=2)
        report = generate_standup(since=self.since, overlay_name=self.OVERLAY)
        payload = report.to_dict()
        yesterday = cast("list[dict[str, object]]", payload["yesterday"])
        assert yesterday[0]["ticket_number"] == "42"
        assert "blockers" in payload

    def test_generator_does_not_mutate_state(self) -> None:
        ticket = self._ticket()
        self._transition(ticket, frm=Ticket.State.STARTED, to=Ticket.State.CODED, hours_ago=2)
        generate_standup(since=self.since, overlay_name=self.OVERLAY)
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.CODED
        assert TicketTransition.objects.count() == 1

    def test_markdown_renders_blockers_and_commits(self) -> None:
        ticket = self._ticket(state=Ticket.State.STARTED)
        self._transition(ticket, frm=Ticket.State.NOT_STARTED, to=Ticket.State.STARTED, hours_ago=2)
        self._attempt(ticket, hours_ago=1, exit_code=1)
        report = generate_standup(
            since=self.since,
            overlay_name=self.OVERLAY,
            commit_collector=lambda _t: ["abc1234 wip"],
        )
        md = report.to_markdown()
        assert "  abc1234 wip" in md
        assert "failed agent run(s) in started" in md
        payload = report.to_dict()
        blockers = cast("list[dict[str, object]]", payload["blockers"])
        assert blockers[0]["failure_count"] == 1
        assert blockers[0]["ticket_number"] == "42"

    def test_empty_report_markdown_has_placeholders(self) -> None:
        md = StandupReport(since=self.since).to_markdown()
        assert "(no phase changes in window)" in md
        assert "(none)" in md

    def test_standup_line_render_singular_run(self) -> None:
        line = StandupLine(
            ticket_number="1",
            ticket_state="coded",
            from_state="started",
            to_state="coded",
            attempt_count=1,
        )
        assert "1 agent run)" in line.render()

    def test_standup_blocker_render(self) -> None:
        blocker = StandupBlocker(ticket_number="9", ticket_state="started", failure_count=2)
        assert blocker.render() == "- TICKET-9: 2 failed agent run(s) in started"

    def test_standup_line_renders_title_inline(self) -> None:
        # #2092: the recap line carries the ticket title inline next to the id,
        # never a bare ``TICKET-N``. Asserting the title text goes RED on the
        # pre-fix renderer.
        line = StandupLine(
            ticket_number="1",
            ticket_state="coded",
            from_state="started",
            to_state="coded",
            attempt_count=1,
            title="fix the broken widget",
        )
        assert "fix the broken widget" in line.render()
        assert "TICKET-1 (fix the broken widget)" in line.render()

    def test_standup_blocker_renders_title_inline(self) -> None:
        blocker = StandupBlocker(ticket_number="9", ticket_state="started", failure_count=2, title="land the eval")
        assert "TICKET-9 (land the eval)" in blocker.render()

    def test_generated_standup_carries_ticket_title(self) -> None:
        ticket = self._ticket()
        ticket.short_description = "fix the broken widget"
        ticket.save(update_fields=["short_description"])
        self._transition(ticket, frm=Ticket.State.STARTED, to=Ticket.State.CODED, hours_ago=2)
        report = generate_standup(since=self.since, overlay_name=self.OVERLAY)
        assert report.yesterday[0].title == "fix the broken widget"
        assert "fix the broken widget" in report.to_markdown()

    def test_default_git_collector_reads_real_log(self) -> None:
        from teatree.utils import git  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            rp = str(repo)
            git.run_strict(repo=rp, args=["init", "-q"])
            git.run_strict(repo=rp, args=["config", "user.email", "t@e.x"])
            git.run_strict(repo=rp, args=["config", "user.name", "t"])
            (repo / "f.txt").write_text("hi")
            git.run_strict(repo=rp, args=["add", "."])
            git.run_strict(repo=rp, args=["commit", "-q", "-m", "standup test commit"])

            ticket = self._ticket(number=321)
            self._transition(ticket, frm=Ticket.State.STARTED, to=Ticket.State.CODED, hours_ago=1)
            # An empty-path worktree is skipped; the real repo is read.
            Worktree.objects.create(
                ticket=ticket,
                overlay=self.OVERLAY,
                repo_path="org/empty",
                branch="b",
                extra={},
            )
            Worktree.objects.create(
                ticket=ticket,
                overlay=self.OVERLAY,
                repo_path="org/repo",
                branch="b",
                extra={"worktree_path": str(repo)},
            )
            report = generate_standup(
                since=timezone.now() - timedelta(days=1),
                overlay_name=self.OVERLAY,
            )
            commits = report.yesterday[0].commits
            assert any("standup test commit" in c for c in commits)

    def test_default_git_collector_degrades_on_git_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            not_a_repo = Path(tmp) / "plain"
            not_a_repo.mkdir()
            ticket = self._ticket(number=654)
            self._transition(ticket, frm=Ticket.State.STARTED, to=Ticket.State.CODED, hours_ago=1)
            Worktree.objects.create(
                ticket=ticket,
                overlay=self.OVERLAY,
                repo_path="org/plain",
                branch="b",
                extra={"worktree_path": str(not_a_repo)},
            )
            report = generate_standup(
                since=timezone.now() - timedelta(days=1),
                overlay_name=self.OVERLAY,
            )
            assert report.yesterday[0].commits == []
