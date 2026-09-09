"""Outage recovery report: find stranded work after a network-outage death (#1764).

A network outage can kill a sub-agent mid-flight, leaving work stranded across
several stores: uncommitted/unpushed branches (data loss), branches with an open
PR, and tickets whose tasks landed FAILED (classified as outage deaths) and
stopped advancing the FSM. ``t3 recover`` gathers all of these into ONE typed
report by composing the primitives that already exist — the boot sweeps,
:mod:`teatree.core.gates.orphan_guard`, :mod:`teatree.core.worktree.reconcile` — plus a
branch -> Worktree -> ticket -> task map. Stranded work is surfaced for SALVAGE
(push the branch to a PR), never auto-captured: there is no recovery snapshot.

``--requeue`` (reopen FAILED tasks) is the only action the operator asks for, and it is
BOUNDED (#4710): a recovery run recovers the incident's casualties, not every failure in
the deployment's history — the dispatcher claims oldest-first, so an unbounded requeue
buries the incident's own fix behind years of dead work.
Gathering is otherwise pure reads EXCEPT the boot sweeps, which run by default
and do write, so the report's own header reports what they recovered rather than
claiming nothing changed.
"""

import datetime as dt
from dataclasses import dataclass, field
from typing import TypedDict

from django.utils import timezone

from teatree.config import clone_root
from teatree.core.gates.orphan_guard import BranchStatus, find_orphans_in_workspace
from teatree.core.modelkit.durations import format_age, format_window
from teatree.core.modelkit.task_parking import PARK_STAMPS
from teatree.core.models import Task, TaskAttempt, Worktree
from teatree.core.worktree.clone_paths import resolve_clone_path
from teatree.core.worktree.reconcile import reconcile_all
from teatree.core.worktree.recovery_sweeps import BootSweepCounts, run_boot_sweeps

_OUTAGE_ERROR_PREFIX = "outage_death:"

#: Recovering an outage means recovering THAT outage's casualties, so the requeue is
#: bounded by default; ``since=None`` is the explicit opt-in to the whole history.
DEFAULT_REQUEUE_WINDOW = dt.timedelta(hours=24)

#: Above this many candidates the reopen is refused rather than performed silently —
#: 186 reopened tasks may not be a side effect of one flag. Raising it means naming the
#: number, so a flood is never a blanket confirmation.
DEFAULT_MAX_REOPEN = 25


class RequeueThresholdError(RuntimeError):
    """Raised when a requeue would reopen more tasks than the caller confirmed."""

    def __init__(self, count: int, max_reopen: int) -> None:
        self.count = count
        self.max_reopen = max_reopen
        super().__init__(
            f"refusing to reopen {count} task(s) — over the --max {max_reopen} ceiling; "
            f"re-run with --max {count} to confirm that number, or narrow --since",
        )


class BootSweepsDict(TypedDict):
    replayed_transitions: int
    reclaimed_claims: int
    reaped_claims: int
    reclaimed_leases: int


class OrphanDict(TypedDict):
    repo: str
    branch: str
    ahead_count: int
    ticket_url: str
    open_pr_url: str


class RequeueExclusionsDict(TypedDict):
    older_than_window: int
    duplicate_phase: int
    live_successor: int


class RequeueDict(TypedDict):
    task_pk: int
    ticket_url: str
    phase: str
    error: str
    is_outage: bool
    failed_at: str


class RecoverReportDict(TypedDict):
    boot_sweeps: BootSweepsDict
    data_loss_risk: list[OrphanDict]
    committed_unpushed: list[OrphanDict]
    open_pr_pending: list[OrphanDict]
    requeue_candidates: list[RequeueDict]
    requeue_window_seconds: int | None
    requeue_excluded: RequeueExclusionsDict
    drift_ticket_pks: list[int]


@dataclass(frozen=True, slots=True)
class OrphanItem:
    """One orphan branch, with the ticket it maps to (if any)."""

    repo: str
    branch: str
    ahead_count: int
    ticket_url: str = ""
    open_pr_url: str = ""


@dataclass(frozen=True, slots=True)
class RequeueCandidate:
    """A FAILED task that can be reopened — an outage death or other failure."""

    task_pk: int
    ticket_url: str
    phase: str
    error: str
    is_outage: bool
    failed_at: dt.datetime | None = None


@dataclass
class RequeueExclusions:
    """Why the FAILED rows that are NOT candidates were left alone."""

    older_than_window: int = 0
    duplicate_phase: int = 0
    live_successor: int = 0

    def to_dict(self) -> RequeueExclusionsDict:
        return RequeueExclusionsDict(
            older_than_window=self.older_than_window,
            duplicate_phase=self.duplicate_phase,
            live_successor=self.live_successor,
        )

    def to_terse(self) -> str:
        parts = [
            f"{self.older_than_window} older than the window",
            f"{self.duplicate_phase} superseded on the same phase",
            f"{self.live_successor} already held by a live task",
        ]
        return ", ".join(parts)


@dataclass
class RecoverReport:
    """Everything ``t3 recover`` found, grouped by recovery action."""

    boot_sweeps: BootSweepCounts = field(default_factory=BootSweepCounts)
    data_loss_risk: list[OrphanItem] = field(default_factory=list)
    committed_unpushed: list[OrphanItem] = field(default_factory=list)
    open_pr_pending: list[OrphanItem] = field(default_factory=list)
    requeue_candidates: list[RequeueCandidate] = field(default_factory=list)
    requeue_window: dt.timedelta | None = DEFAULT_REQUEUE_WINDOW
    requeue_excluded: RequeueExclusions = field(default_factory=RequeueExclusions)
    drift_ticket_pks: list[int] = field(default_factory=list)

    @property
    def has_findings(self) -> bool:
        return any(
            (
                self.data_loss_risk,
                self.committed_unpushed,
                self.open_pr_pending,
                self.requeue_candidates,
                self.drift_ticket_pks,
            ),
        )

    def to_dict(self) -> RecoverReportDict:
        return RecoverReportDict(
            boot_sweeps=BootSweepsDict(
                replayed_transitions=self.boot_sweeps.replayed_transitions,
                reclaimed_claims=self.boot_sweeps.reclaimed_claims,
                reaped_claims=self.boot_sweeps.reaped_claims,
                reclaimed_leases=self.boot_sweeps.reclaimed_leases,
            ),
            data_loss_risk=[self._orphan_dict(o) for o in self.data_loss_risk],
            committed_unpushed=[self._orphan_dict(o) for o in self.committed_unpushed],
            open_pr_pending=[self._orphan_dict(o) for o in self.open_pr_pending],
            requeue_candidates=[
                RequeueDict(
                    task_pk=c.task_pk,
                    ticket_url=c.ticket_url,
                    phase=c.phase,
                    error=c.error,
                    is_outage=c.is_outage,
                    failed_at=c.failed_at.isoformat() if c.failed_at else "",
                )
                for c in self.requeue_candidates
            ],
            requeue_window_seconds=(None if self.requeue_window is None else int(self.requeue_window.total_seconds())),
            requeue_excluded=self.requeue_excluded.to_dict(),
            drift_ticket_pks=self.drift_ticket_pks,
        )

    @staticmethod
    def _orphan_dict(orphan: OrphanItem) -> OrphanDict:
        return OrphanDict(
            repo=orphan.repo,
            branch=orphan.branch,
            ahead_count=orphan.ahead_count,
            ticket_url=orphan.ticket_url,
            open_pr_url=orphan.open_pr_url,
        )

    def _header(self, *, dry_run: bool) -> str:
        """The mode line, whose no-change claim is read off the sweeps rather than assumed.

        The boot sweeps run on the default path and DO write, so a flat "nothing
        changed" was false exactly when the operator most needed to know otherwise.
        """
        if not dry_run:
            return "t3 recover — applied"
        recovered = self.boot_sweeps.changed_rows
        if recovered:
            return f"t3 recover — DRY RUN except the boot sweeps, which recovered {recovered} row(s)"
        return "t3 recover — DRY RUN (nothing changed)"

    def to_terse(self, *, dry_run: bool) -> str:
        header = self._header(dry_run=dry_run)
        sweeps = self.boot_sweeps
        sweep_line = (
            f"boot sweeps: replayed={sweeps.replayed_transitions} "
            f"reclaimed={sweeps.reclaimed_claims} reaped={sweeps.reaped_claims} "
            f"leases={sweeps.reclaimed_leases}"
        )
        lines = [header, sweep_line]
        lines += self._render_orphans("Data-loss risk (unpushed)", self.data_loss_risk)
        lines += self._render_orphans("Committed-unpushed (pushed, no PR)", self.committed_unpushed)
        lines += self._render_orphans("Open-PR pending", self.open_pr_pending)
        if self.requeue_candidates:
            now = timezone.now()
            lines.append(
                f"Re-queue candidates ({len(self.requeue_candidates)}, {self._window_phrase()}; "
                f"excluded: {self.requeue_excluded.to_terse()}):",
            )
            lines += [
                f"  task TODO-{c.task_pk} {c.phase or '(no phase)'} "
                f"{'[outage]' if c.is_outage else '[failed]'} {c.ticket_url or '(no url)'} "
                f"({format_age(c.failed_at, now=now)}) — {c.error}"
                for c in self.requeue_candidates
            ]
        if self.drift_ticket_pks:
            lines.append(f"Reconcile drift on tickets: {', '.join(f'teatree#{pk}' for pk in self.drift_ticket_pks)}")
        if not self.has_findings:
            lines.append("(no stranded work found)")
        return "\n".join(lines)

    def _window_phrase(self) -> str:
        if self.requeue_window is None:
            return "any age"
        return f"within {format_window(self.requeue_window)}"

    def requeue_preview(self) -> str:
        """What ``--requeue`` is about to do, stated BEFORE it does it."""
        candidates = self.requeue_candidates
        if not candidates:
            return f"Will reopen 0 task(s) — nothing failed {self._window_phrase()}."
        now = timezone.now()
        tickets = {c.ticket_url for c in candidates}
        dated = sorted(c.failed_at for c in candidates if c.failed_at is not None)
        ages = [format_age(dated[0], now=now), format_age(dated[-1], now=now)] if dated else []
        span = " … ".join(dict.fromkeys(ages)) if ages else "undated"
        return (
            f"Will reopen {len(candidates)} task(s) across {len(tickets)} ticket(s), "
            f"failed {span} ({self._window_phrase()})."
        )

    @staticmethod
    def _render_orphans(title: str, orphans: list[OrphanItem]) -> list[str]:
        if not orphans:
            return []
        out = [f"{title} ({len(orphans)}):"]
        for o in orphans:
            ref = o.open_pr_url or o.ticket_url or "(no url)"
            out.append(f"  {o.repo} {o.branch} (+{o.ahead_count}) -> {ref}")
        return out


def _branch_to_ticket_url() -> dict[tuple[str, str], str]:
    """Map ``(clone_path, branch)`` to the ticket issue_url that owns it."""
    workspace = clone_root()
    mapping: dict[tuple[str, str], str] = {}
    for wt in Worktree.objects.select_related("ticket"):
        clone = resolve_clone_path(workspace, wt)
        if clone is None:
            continue
        mapping[str(clone), wt.branch] = wt.ticket.issue_url
    return mapping


def _classify_orphans(report: RecoverReport) -> None:
    ticket_urls = _branch_to_ticket_url()
    for branch_report in find_orphans_in_workspace():
        item = OrphanItem(
            repo=branch_report.repo,
            branch=branch_report.branch,
            ahead_count=branch_report.ahead_count,
            ticket_url=ticket_urls.get((branch_report.repo, branch_report.branch), ""),
            open_pr_url=branch_report.open_pr_url,
        )
        # find_orphans_in_workspace only yields the three orphan statuses, so the
        # final bucket is the open-PR case (no SYNCED leaks through).
        if branch_report.status == BranchStatus.UNPUSHED_ORPHAN:
            report.data_loss_risk.append(item)
        elif branch_report.status == BranchStatus.PUSHED_ORPHAN:
            report.committed_unpushed.append(item)
        else:
            report.open_pr_pending.append(item)


def _reopenable_failed_tasks() -> list[Task]:
    """The FAILED rows a requeue may touch at all, newest first, attempts prefetched.

    Excludes the DELIBERATELY parked ones — a halted task already paged a human and a
    superseded one has a live successor holding its phase, so reopening either undoes a
    decision rather than recovering a casualty.
    """
    queryset = Task.objects.filter(status=Task.Status.FAILED)
    for stamp in PARK_STAMPS:
        queryset = queryset.exclude(execution_reason__contains=stamp)
    return list(queryset.select_related("ticket").prefetch_related("attempts").order_by("-pk"))


def _last_attempt(task: Task) -> TaskAttempt | None:
    attempts = sorted(task.attempts.all(), key=lambda a: a.pk)  # ty: ignore[unresolved-attribute]
    return attempts[-1] if attempts else None


def _failure_instant(task: Task) -> dt.datetime | None:
    """When this task last failed — an attempt killed mid-flight records no ``ended_at``."""
    attempt = _last_attempt(task)
    if attempt is not None:
        return attempt.ended_at or attempt.started_at
    return task.created_at


def _live_phase_keys() -> set[tuple[int, str]]:
    """The ``(ticket, phase)`` pairs a PENDING/CLAIMED task already holds."""
    return set(
        Task.objects.filter(status__in=Task.Status.active()).values_list("ticket_id", "phase"),
    )


def _collect_requeue_candidates(report: RecoverReport, *, since: dt.timedelta | None) -> None:
    """Bound the candidates to *since* and to ONE task per ``(ticket, phase)`` (#4710).

    Newest-first, so the survivor of a de-duplicated phase is the attempt carrying the
    most current context; the report itself is re-sorted into task order for reading.
    """
    cutoff = None if since is None else timezone.now() - since
    live_keys = _live_phase_keys()
    claimed_keys: set[tuple[int, str]] = set()
    for task in _reopenable_failed_tasks():
        if task.ticket.is_terminal:
            continue
        # An unknown-overlay task can never be dispatched — reopening it would
        # re-crash on every drain (souliane/teatree#1959 poison pill).
        if not task.ticket.has_dispatchable_overlay():
            continue
        failed_at = _failure_instant(task)
        # An undated failure cannot be PROVED inside the window, so it stays out of it.
        if cutoff is not None and (failed_at is None or failed_at < cutoff):
            report.requeue_excluded.older_than_window += 1
            continue
        key = (task.ticket.pk, task.phase)
        if key in live_keys:
            report.requeue_excluded.live_successor += 1
            continue
        if key in claimed_keys:
            report.requeue_excluded.duplicate_phase += 1
            continue
        claimed_keys.add(key)
        attempt = _last_attempt(task)
        error = attempt.error if attempt else ""
        report.requeue_candidates.append(
            RequeueCandidate(
                task_pk=task.pk,
                ticket_url=task.ticket.issue_url,
                phase=task.phase,
                error=error,
                is_outage=error.startswith(_OUTAGE_ERROR_PREFIX),
                failed_at=failed_at,
            ),
        )
    report.requeue_candidates.sort(key=lambda c: c.task_pk)


def gather_recover_report(
    *,
    run_sweeps: bool = True,
    since: dt.timedelta | None = DEFAULT_REQUEUE_WINDOW,
) -> RecoverReport:
    """Compose the full recovery report from the existing recovery primitives.

    Pure reads except the boot sweeps, which are themselves idempotent recovery
    (replay dropped transitions, reclaim orphaned claims, reap stale claims) and
    are the documented boot/tick behaviour — they run by default so a stalled
    ledger is rescued before the report is built. ``run_sweeps=False`` skips them
    for a strictly read-only inspection. ``since=None`` drops the requeue window.
    """
    report = RecoverReport(
        boot_sweeps=run_boot_sweeps() if run_sweeps else BootSweepCounts(),
        requeue_window=since,
    )
    _classify_orphans(report)
    _collect_requeue_candidates(report, since=since)
    report.drift_ticket_pks = sorted(reconcile_all().keys())
    return report


def requeue_failed_tasks(report: RecoverReport, *, max_reopen: int = DEFAULT_MAX_REOPEN) -> list[int]:
    """Reopen the genuinely-incomplete FAILED tasks in *report*. Returns reopened pks.

    Only reopens tasks whose ticket is still non-terminal (the candidates the
    report already filtered to) and whose status is still FAILED at write time —
    a task completed by a concurrent actor between gather and requeue is skipped.
    Past *max_reopen* candidates nothing is reopened: confirming a flood means naming
    its size, so it is always a number someone chose rather than a blanket yes.
    """
    if len(report.requeue_candidates) > max_reopen:
        raise RequeueThresholdError(len(report.requeue_candidates), max_reopen)
    reopened: list[int] = []
    for candidate in report.requeue_candidates:
        task = Task.objects.filter(pk=candidate.task_pk, status=Task.Status.FAILED).first()
        if task is None:
            continue
        task.reopen()
        reopened.append(task.pk)
    return reopened
