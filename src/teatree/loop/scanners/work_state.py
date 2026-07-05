"""WorkStateScanner — surface committed-unpushed / done-unmerged / duplicate-scope drift each tick.

SELFCATCH-1. The three probes (``commits_absent_from_all_remotes`` /
``branch_redundancy`` / ``find_foreign_issue_worktrees``) already existed but only
ran at teardown or provisioning time, never autonomously in a loop. This scanner
runs them over every ticket each tick via :func:`reconcile_work_state_all` and
emits one ``workstate.drift`` signal per finding, so the factory surfaces its own
orphaned work — committed-but-unpushed commits, done-but-unmerged branches,
duplicate-scope worktrees — at the next tick instead of when a human notices.

Read-only: it SURFACES drift (emits signals into the action-needed statusline
zone); it never auto-pushes or auto-deletes — destructive remediation stays gated.

Fail-closed on two levels: the per-probe fail-closed semantics live in
:mod:`teatree.core.reconcile` (an inconclusive probe is a finding, never a silent
pass), and the scanner fails closed at its own boundary too — a
:func:`reconcile_work_state_all` that raises emits a ``workstate.probe_error``
finding rather than a silent green tick.

The Django models are resolved lazily inside :meth:`scan` so this module stays
importable before ``django.setup()`` runs (the loop subapp is imported at CLI
startup, before Django is ready), mirroring the other DB-backed scanners.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from teatree.loop.scanners.base import ScanSignal

if TYPE_CHECKING:
    from teatree.core.reconcile import DoneButUnmerged, Drift, DuplicateScope, UnpushedWork


@dataclass(slots=True)
class WorkStateScanner:
    """Emit one ``workstate.drift`` signal per work-tracking-truth finding each tick.

    ``limit`` caps signals per tick so a large backlog spreads across ticks rather
    than flooding one statusline render (mirrors the other DB-backed scanners).
    """

    name: str = "work_state"
    limit: int = 200

    def scan(self) -> list[ScanSignal]:
        from teatree.core.reconcile import reconcile_work_state_all  # noqa: PLC0415 — Django models

        try:
            drifts = reconcile_work_state_all()
        except Exception as exc:  # noqa: BLE001 — fail closed: an errored sweep is a finding, not a silent green tick
            return [
                ScanSignal(
                    kind="workstate.probe_error",
                    summary=f"work-state reconcile failed: {type(exc).__name__}: {exc}",
                    payload={"error": f"{type(exc).__name__}: {exc}"},
                ),
            ]
        signals: list[ScanSignal] = []
        for ticket_pk, drift in sorted(drifts.items()):
            signals.extend(_signals_for_drift(ticket_pk, drift))
        return signals[: self.limit]


def _signals_for_drift(ticket_pk: int, drift: "Drift") -> list[ScanSignal]:
    """Translate one ticket's work-state findings into ``workstate.drift`` signals."""
    signals: list[ScanSignal] = []
    signals.extend(_unpushed_signal(ticket_pk, unpushed) for unpushed in drift.unpushed_work)
    signals.extend(_done_signal(ticket_pk, done) for done in drift.done_but_unmerged)
    signals.extend(_duplicate_signal(ticket_pk, dup) for dup in drift.duplicate_scopes)
    return signals


def _unpushed_signal(ticket_pk: int, unpushed: "UnpushedWork") -> ScanSignal:
    detail = (
        f"{len(unpushed.shas)} commit(s) absent from all remotes"
        if unpushed.shas
        else f"pushed-state probe inconclusive ({unpushed.probe_error})"
    )
    return ScanSignal(
        kind="workstate.drift",
        summary=f"unpushed work on {unpushed.branch} (wt#{unpushed.worktree_pk}): {detail}",
        payload={
            "finding": "unpushed_work",
            "ticket_pk": ticket_pk,
            "worktree_pk": unpushed.worktree_pk,
            "branch": unpushed.branch,
            "shas": unpushed.shas,
            "probe_error": unpushed.probe_error,
        },
    )


def _done_signal(ticket_pk: int, done: "DoneButUnmerged") -> ScanSignal:
    return ScanSignal(
        kind="workstate.drift",
        summary=f"ticket #{ticket_pk} marked done but branch {done.branch} unmerged: {done.reason}",
        payload={"finding": "done_but_unmerged", "ticket_pk": ticket_pk, "branch": done.branch, "reason": done.reason},
    )


def _duplicate_signal(ticket_pk: int, dup: "DuplicateScope") -> ScanSignal:
    return ScanSignal(
        kind="workstate.drift",
        summary=f"duplicate scope for issue {dup.issue_number}: {len(dup.paths)} worktree dirs",
        payload={
            "finding": "duplicate_scope",
            "ticket_pk": ticket_pk,
            "issue_number": dup.issue_number,
            "paths": [str(path) for path in dup.paths],
        },
    )
