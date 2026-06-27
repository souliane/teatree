"""Outage recovery report: find stranded work after a network-outage death (#1764).

A network outage can kill a sub-agent mid-flight, leaving work stranded across
several stores: uncommitted/unpushed branches (data loss), branches with an open
PR, captured-but-unrestored recovery snapshots, and tickets whose tasks landed
FAILED (classified as outage deaths) and stopped advancing the FSM. ``t3 recover``
gathers all of these into ONE typed report by composing the primitives that
already exist — the boot sweeps, :mod:`teatree.core.gates.orphan_guard`,
:mod:`teatree.core.reconcile` — plus a branch -> Worktree -> ticket -> task map.

Default is a DRY-RUN: gathering is pure reads, and the report mutates nothing.
``--requeue`` (reopen FAILED tasks) and ``--snapshot`` (force-capture dirty
worktrees) are the only mutating actions, applied explicitly by the command.
"""

import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypedDict

from teatree.config import clone_root
from teatree.core.clone_paths import resolve_clone_path
from teatree.core.gates.orphan_guard import BranchStatus, find_orphans_in_workspace
from teatree.core.models import Task, Worktree
from teatree.core.reconcile import reconcile_all
from teatree.core.recovery_sweeps import BootSweepCounts, run_boot_sweeps
from teatree.core.worktree_snapshot import capture_worktree_snapshot

_OUTAGE_ERROR_PREFIX = "outage_death:"
_SNAPSHOT_PREFIX = "t3-recover-"


class BootSweepsDict(TypedDict):
    replayed_transitions: int
    reclaimed_claims: int
    reaped_claims: int


class OrphanDict(TypedDict):
    repo: str
    branch: str
    ahead_count: int
    ticket_url: str
    open_pr_url: str


class RequeueDict(TypedDict):
    task_pk: int
    ticket_url: str
    phase: str
    error: str
    is_outage: bool


class RecoverReportDict(TypedDict):
    boot_sweeps: BootSweepsDict
    data_loss_risk: list[OrphanDict]
    committed_unpushed: list[OrphanDict]
    open_pr_pending: list[OrphanDict]
    stranded_snapshots: list[str]
    requeue_candidates: list[RequeueDict]
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
class StrandedSnapshot:
    """A captured recovery artifact still sitting in the temp dir."""

    path: Path


@dataclass(frozen=True, slots=True)
class RequeueCandidate:
    """A FAILED task that can be reopened — an outage death or other failure."""

    task_pk: int
    ticket_url: str
    phase: str
    error: str
    is_outage: bool


@dataclass
class RecoverReport:
    """Everything ``t3 recover`` found, grouped by recovery action."""

    boot_sweeps: BootSweepCounts = field(default_factory=BootSweepCounts)
    data_loss_risk: list[OrphanItem] = field(default_factory=list)
    committed_unpushed: list[OrphanItem] = field(default_factory=list)
    open_pr_pending: list[OrphanItem] = field(default_factory=list)
    stranded_snapshots: list[StrandedSnapshot] = field(default_factory=list)
    requeue_candidates: list[RequeueCandidate] = field(default_factory=list)
    drift_ticket_pks: list[int] = field(default_factory=list)

    @property
    def has_findings(self) -> bool:
        return any(
            (
                self.data_loss_risk,
                self.committed_unpushed,
                self.open_pr_pending,
                self.stranded_snapshots,
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
            ),
            data_loss_risk=[self._orphan_dict(o) for o in self.data_loss_risk],
            committed_unpushed=[self._orphan_dict(o) for o in self.committed_unpushed],
            open_pr_pending=[self._orphan_dict(o) for o in self.open_pr_pending],
            stranded_snapshots=[str(s.path) for s in self.stranded_snapshots],
            requeue_candidates=[
                RequeueDict(
                    task_pk=c.task_pk,
                    ticket_url=c.ticket_url,
                    phase=c.phase,
                    error=c.error,
                    is_outage=c.is_outage,
                )
                for c in self.requeue_candidates
            ],
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

    def to_terse(self, *, dry_run: bool) -> str:
        header = "t3 recover — DRY RUN (nothing changed)" if dry_run else "t3 recover — applied"
        sweeps = self.boot_sweeps
        sweep_line = (
            f"boot sweeps: replayed={sweeps.replayed_transitions} "
            f"reclaimed={sweeps.reclaimed_claims} reaped={sweeps.reaped_claims}"
        )
        lines = [header, sweep_line]
        lines += self._render_orphans("Data-loss risk (unpushed)", self.data_loss_risk)
        lines += self._render_orphans("Committed-unpushed (pushed, no PR)", self.committed_unpushed)
        lines += self._render_orphans("Open-PR pending", self.open_pr_pending)
        if self.stranded_snapshots:
            lines.append(f"Stranded snapshots ({len(self.stranded_snapshots)}):")
            lines += [f"  {s.path}" for s in self.stranded_snapshots]
        if self.requeue_candidates:
            lines.append(f"Re-queue candidates ({len(self.requeue_candidates)}):")
            lines += [
                f"  task TODO-{c.task_pk} {c.phase or '(no phase)'} "
                f"{'[outage]' if c.is_outage else '[failed]'} {c.ticket_url or '(no url)'} — {c.error}"
                for c in self.requeue_candidates
            ]
        if self.drift_ticket_pks:
            lines.append(f"Reconcile drift on tickets: {', '.join(f'teatree#{pk}' for pk in self.drift_ticket_pks)}")
        if not self.has_findings:
            lines.append("(no stranded work found)")
        return "\n".join(lines)

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


def _collect_stranded_snapshots(report: RecoverReport) -> None:
    temp_root = Path(tempfile.gettempdir())
    if not temp_root.is_dir():
        return
    for entry in sorted(temp_root.iterdir()):
        if entry.is_dir() and entry.name.startswith(_SNAPSHOT_PREFIX):
            report.stranded_snapshots.append(StrandedSnapshot(path=entry))


def _collect_requeue_candidates(report: RecoverReport) -> None:
    for task in Task.objects.filter(status=Task.Status.FAILED).select_related("ticket").order_by("pk"):
        if task.ticket.is_terminal:
            continue
        # An unknown-overlay task can never be dispatched — reopening it would
        # re-crash on every drain (souliane/teatree#1959 poison pill).
        if not task.ticket.has_dispatchable_overlay():
            continue
        last = task.attempts.order_by("-pk").first()
        error = last.error if last else ""
        report.requeue_candidates.append(
            RequeueCandidate(
                task_pk=task.pk,
                ticket_url=task.ticket.issue_url,
                phase=task.phase,
                error=error,
                is_outage=error.startswith(_OUTAGE_ERROR_PREFIX),
            ),
        )


def gather_recover_report(*, run_sweeps: bool = True) -> RecoverReport:
    """Compose the full recovery report from the existing recovery primitives.

    Pure reads except the boot sweeps, which are themselves idempotent recovery
    (replay dropped transitions, reclaim orphaned claims, reap stale claims) and
    are the documented boot/tick behaviour — they run by default so a stalled
    ledger is rescued before the report is built. ``run_sweeps=False`` skips them
    for a strictly read-only inspection.
    """
    report = RecoverReport(boot_sweeps=run_boot_sweeps() if run_sweeps else BootSweepCounts())
    _classify_orphans(report)
    _collect_stranded_snapshots(report)
    _collect_requeue_candidates(report)
    report.drift_ticket_pks = sorted(reconcile_all().keys())
    return report


def requeue_failed_tasks(report: RecoverReport) -> list[int]:
    """Reopen the genuinely-incomplete FAILED tasks in *report*. Returns reopened pks.

    Only reopens tasks whose ticket is still non-terminal (the candidates the
    report already filtered to) and whose status is still FAILED at write time —
    a task completed by a concurrent actor between gather and requeue is skipped.
    """
    reopened: list[int] = []
    for candidate in report.requeue_candidates:
        task = Task.objects.filter(pk=candidate.task_pk, status=Task.Status.FAILED).first()
        if task is None:
            continue
        task.reopen()
        reopened.append(task.pk)
    return reopened


def force_capture_snapshots() -> list[Path]:
    """Capture a snapshot of every dirty/unpushed tracked worktree. Returns artifact dirs."""
    workspace = clone_root()
    captured: list[Path] = []
    for wt in Worktree.objects.select_related("ticket"):
        clone = resolve_clone_path(workspace, wt)
        wt_path = wt.worktree_path
        if clone is None or not wt_path:
            continue
        recovery_dir = capture_worktree_snapshot(
            clone,
            wt_path,
            branch=wt.branch,
            label=wt.ticket.ticket_number,
        )
        if recovery_dir is not None:
            captured.append(recovery_dir)
    return captured
