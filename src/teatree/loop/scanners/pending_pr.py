"""Scanner that drains the PR obligations ``ensure-pr`` deferred at push time.

``ensure-pr`` runs in the git PRE-push hook and defers when the remote ref is
absent or stale. Git has no client-side post-push hook, so nothing re-ran the
deferral — the branch shipped with no PR. Each deferral now owes a
:class:`~teatree.core.models.pending_pull_request.PendingPullRequest`, and this
scanner is the drain: once per dispatch tick it re-runs the idempotent
``ensure-pr`` path per owed branch, now that the push has landed and the remote
ref is visible.

The cross-tick peer of :class:`UndeliveredNotifyScanner` and
:class:`DeferredQuestionPosterScanner`: the same durable-row-then-drain shape,
with the push deadlock as the durability trigger. It emits a
:class:`ScanSignal` only when it actually discharges something; an obligation
that survives ``MAX_DRAIN_ATTEMPTS`` drains is surfaced by ``t3 doctor check``,
never retried in silence.
"""

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

from django.db import OperationalError, ProgrammingError
from django.utils import timezone

from teatree.core.models.pending_pull_request import MAX_DRAIN_ATTEMPTS
from teatree.loop.scanners.base import ScanSignal

if TYPE_CHECKING:
    from teatree.core.models import PendingPullRequest

logger = logging.getLogger(__name__)


def _due(row: "PendingPullRequest") -> bool:
    if row.drain_attempts < MAX_DRAIN_ATTEMPTS or row.last_attempt_at is None:
        return True
    minutes = min(5 * 2 ** min(row.drain_attempts - MAX_DRAIN_ATTEMPTS, 9), 24 * 60)
    return timezone.now() >= row.last_attempt_at + timedelta(minutes=minutes)


@dataclass(slots=True)
class PendingPrDrainScanner:
    #: Oldest obligation first, so a backlog past this cap can never starve the
    #: branch that has been waiting longest for its PR.
    limit: int = 50
    name: str = "pending_pr_drain"

    def scan(self) -> list[ScanSignal]:
        from teatree.core.models import PendingPullRequest  # noqa: PLC0415 — deferred: loaded at tick time
        from teatree.core.worktree.stuck_pr_notice import (  # noqa: PLC0415 — owner cards use ORM at tick time
            ask_owner_about_stuck_prs,
            discharge_stopped,
            settle_discharged_stuck_pr_notices,
        )

        try:
            retired = PendingPullRequest.objects.retire_absent()
            owed: list[PendingPullRequest] = []
            for row in PendingPullRequest.objects.order_by("deferred_at").iterator(chunk_size=100):
                if len(owed) >= self.limit:
                    break
                if _due(row):
                    owed.append(row)
        except (OperationalError, ProgrammingError):
            logger.info("PendingPrDrainScanner: PendingPullRequest unavailable (DB not migrated yet) — skipping")
            return []
        signals = self._retirement_signals(retired)
        discharge_stopped(self._overdue())
        discharged = sum(1 for row in owed if self._drain_one(row))
        settle_discharged_stuck_pr_notices()
        ask_owner_about_stuck_prs(self._overdue())
        if discharged:
            signals.append(
                ScanSignal(
                    kind="pending_pr.drained",
                    summary=f"opened {discharged}/{len(owed)} deferred pull request(s)",
                    payload={"discharged": discharged, "owed": len(owed)},
                ),
            )
        return signals

    def _overdue(self) -> "list[PendingPullRequest]":
        from teatree.core.models import PendingPullRequest  # noqa: PLC0415 — deferred: loaded at tick time

        return list(PendingPullRequest.objects.overdue().order_by("deferred_at")[: self.limit])

    @staticmethod
    def _retirement_signals(retired: list[tuple[int, str, str]]) -> list[ScanSignal]:
        """Announce the obligations whose checkout is gone — once, since the row is deleted."""
        if not retired:
            return []
        for pk, branch, repo_path in retired:
            logger.warning(
                "PendingPrDrainScanner: retired obligation %s for %s — checkout %s is gone", pk, branch, repo_path
            )
        return [
            ScanSignal(
                kind="pending_pr.retired",
                summary=f"retired {len(retired)} pull-request obligation(s) whose checkout is gone",
                payload={"retired": [{"id": pk, "branch": branch, "repo_path": path} for pk, branch, path in retired]},
            ),
        ]

    @staticmethod
    def _drain_one(row: "PendingPullRequest") -> bool:
        """Re-run ``ensure-pr`` for one owed branch; ``True`` when the obligation is discharged.

        ``ensure-pr`` is the whole creation path — classification included — so a
        branch that got squash-merged while the obligation sat here is discharged
        without opening a spurious PR. A result that still owes, or that errored,
        keeps the row and ages it toward the doctor FAIL.
        """
        from django.core.management import call_command  # noqa: PLC0415 — deferred: Django import at call time

        from teatree.core.models.pending_pull_request import (  # noqa: PLC0415 — deferred: loaded at tick time
            PendingPullRequest,
            settles_obligation,
        )

        try:
            result = call_command("pr", "ensure-pr", repo=row.repo_path, branch=row.branch)
        except Exception as exc:
            logger.exception("PendingPrDrainScanner: ensure-pr failed for %s@%s", row.branch, row.repo_path)
            row.record_failed_drain(error=f"{type(exc).__name__}: {exc}")
            return False
        if not isinstance(result, dict):
            row.record_failed_drain(error=f"ensure-pr returned {type(result).__name__}, not a result")
            return False
        if not settles_obligation(result):
            row.record_failed_drain(error=str(result.get("error") or result.get("skipped") or ""))
            return False
        PendingPullRequest.objects.discharge(repo_path=row.repo_path, branch=row.branch)
        return True
