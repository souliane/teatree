"""Auto-generated standup, derived entirely from existing models (issue #563).

Cross-references data the FSM and loop already record — no human input,
no new models, no migrations. ``TicketTransition`` gives the phase
changes inside the time window; ``TaskAttempt`` gives the agent runs
(count, failures); ``git log`` per active worktree repo is injected via
*commit_collector* so the generator stays read-only and hermetic under
test.

Every query here is **read-only**. The generator never transitions a
:class:`Ticket` nor writes any row. ``git log`` is the only external
touch and runs through the standard read-only :mod:`teatree.utils.git`
helper (no checkout, fetch, or write).
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import TypedDict

from django.db.models import Count, Q

from teatree.core.models.task import TaskAttempt
from teatree.core.models.ticket import Ticket
from teatree.core.models.transition import TicketTransition
from teatree.core.ref_render import render_ref
from teatree.utils import git

CommitCollector = Callable[[Ticket], list[str]]


class StandupLineDict(TypedDict):
    ticket_number: str
    ticket_state: str
    from_state: str
    to_state: str
    attempt_count: int
    commits: list[str]
    title: str


class StandupBlockerDict(TypedDict):
    ticket_number: str
    ticket_state: str
    failure_count: int
    title: str


class StandupReportDict(TypedDict):
    since: str
    yesterday: list[StandupLineDict]
    blockers: list[StandupBlockerDict]
    markdown: str


@dataclass(frozen=True, slots=True)
class StandupLine:
    """One ticket's activity inside the standup window."""

    ticket_number: str
    ticket_state: str
    from_state: str
    to_state: str
    attempt_count: int
    commits: list[str] = field(default_factory=list)
    title: str = ""

    def to_dict(self) -> StandupLineDict:
        return StandupLineDict(
            ticket_number=self.ticket_number,
            ticket_state=self.ticket_state,
            from_state=self.from_state,
            to_state=self.to_state,
            attempt_count=self.attempt_count,
            commits=list(self.commits),
            title=self.title,
        )

    def render(self) -> str:
        ref = render_ref(f"TICKET-{self.ticket_number}", title=self.title)
        head = f"- {ref}: {self.from_state} → {self.to_state}"
        meta = f" ({self.attempt_count} agent run{'s' if self.attempt_count != 1 else ''})"
        lines = [head + meta]
        lines.extend(f"  {commit}" for commit in self.commits)
        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class StandupBlocker:
    """A ticket with a failed agent run inside the window."""

    ticket_number: str
    ticket_state: str
    failure_count: int
    title: str = ""

    def to_dict(self) -> StandupBlockerDict:
        return StandupBlockerDict(
            ticket_number=self.ticket_number,
            ticket_state=self.ticket_state,
            failure_count=self.failure_count,
            title=self.title,
        )

    def render(self) -> str:
        ref = render_ref(f"TICKET-{self.ticket_number}", title=self.title)
        return f"- {ref}: {self.failure_count} failed agent run(s) in {self.ticket_state}"


@dataclass(frozen=True, slots=True)
class StandupReport:
    """Structured, read-only daily update for a time window."""

    since: datetime
    yesterday: list[StandupLine] = field(default_factory=list)
    blockers: list[StandupBlocker] = field(default_factory=list)

    def to_dict(self) -> StandupReportDict:
        return StandupReportDict(
            since=self.since.isoformat(),
            yesterday=[line.to_dict() for line in self.yesterday],
            blockers=[blocker.to_dict() for blocker in self.blockers],
            markdown=self.to_markdown(),
        )

    def to_markdown(self) -> str:
        body = [line.render() for line in self.yesterday] or ["- (no phase changes in window)"]
        blockers = [blocker.render() for blocker in self.blockers] or ["- (none)"]
        return "\n".join(["## Yesterday", *body, "", "## Blockers", *blockers])


def _git_commits_since(since: datetime) -> CommitCollector:
    """Default collector: ``git log --since`` per active worktree repo path.

    Read-only — :func:`teatree.utils.git.log_oneline` runs ``git log`` only.
    Any git failure (missing repo, detached path) degrades to no commits
    rather than aborting the standup: :func:`teatree.utils.git.run` accepts
    any exit code and returns empty output on failure, so a bad path simply
    contributes nothing.
    """

    def collect(ticket: Ticket) -> list[str]:
        out: list[str] = []
        for worktree in ticket.worktrees.all():  # ty: ignore[unresolved-attribute]
            path = worktree.worktree_path
            if not path:
                continue
            log = git.run(repo=path, args=["log", "--oneline", f"--since={since.isoformat()}"])
            out.extend(line for line in log.splitlines() if line.strip())
        return out

    return collect


def generate_standup(
    *,
    since: datetime,
    overlay_name: str = "",
    commit_collector: CommitCollector | None = None,
) -> StandupReport:
    """Build a :class:`StandupReport` for activity at or after *since*.

    Pure read path: aggregates :class:`TicketTransition` and
    :class:`TaskAttempt` rows, never mutating state. *commit_collector*
    defaults to a ``git log --since`` reader and is injected in tests.
    """
    collector = commit_collector or _git_commits_since(since)

    transitions = (
        TicketTransition.objects.filter(created_at__gte=since)
        .select_related("ticket")
        .order_by("ticket_id", "-created_at")
    )
    if overlay_name:
        transitions = transitions.filter(ticket__overlay=overlay_name)

    attempt_counts = _attempt_counts_by_ticket(since=since, overlay_name=overlay_name)

    seen: set[int] = set()
    lines: list[StandupLine] = []
    for tr in transitions:
        if tr.ticket_id in seen:
            continue
        seen.add(tr.ticket_id)
        ticket = tr.ticket
        counts = attempt_counts.get(tr.ticket_id, (0, 0))
        lines.append(
            StandupLine(
                ticket_number=ticket.ticket_number,
                ticket_state=ticket.state,
                from_state=tr.from_state,
                to_state=tr.to_state,
                attempt_count=counts[0],
                commits=collector(ticket),
                title=ticket.short_description,
            ),
        )

    return StandupReport(since=since, yesterday=lines, blockers=_blockers(attempt_counts))


def _attempt_counts_by_ticket(*, since: datetime, overlay_name: str) -> dict[int, tuple[int, int]]:
    """Map ``ticket_id -> (total_attempts, failed_attempts)`` within the window."""
    qs = TaskAttempt.objects.filter(started_at__gte=since)
    if overlay_name:
        qs = qs.filter(task__ticket__overlay=overlay_name)
    rows = (
        qs.values("task__ticket_id")
        .annotate(
            total=Count("id"),
            failed=Count("id", filter=Q(exit_code__gt=0)),
        )
        .order_by()
    )
    return {row["task__ticket_id"]: (row["total"], row["failed"]) for row in rows}


def _blockers(attempt_counts: dict[int, tuple[int, int]]) -> list[StandupBlocker]:
    blocked_ids = [tid for tid, (_total, failed) in attempt_counts.items() if failed > 0]
    if not blocked_ids:
        return []
    # ``blocked_ids`` come from ``TaskAttempt`` rows; the FK + CASCADE
    # guarantees every id resolves to a live Ticket, so no missing-key
    # guard is needed.
    tickets = Ticket.objects.filter(pk__in=blocked_ids).only("id", "state", "issue_url", "short_description")
    return [
        StandupBlocker(
            ticket_number=ticket.ticket_number,
            ticket_state=ticket.state,
            failure_count=attempt_counts[ticket.pk][1],
            title=ticket.short_description,
        )
        for ticket in tickets
    ]
