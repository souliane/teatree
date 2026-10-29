"""Recheck old pending work before its recorded premise can silently age out."""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, cast

from django.apps import apps
from django.db import transaction
from django.utils import timezone

from teatree.backends.loader import get_code_host_for_url
from teatree.core.gates.plan_currency_gate import check_plan_current
from teatree.core.modelkit.task_failure_taxonomy import SUPERSEDED_PREFIX
from teatree.core.models.errors import NoCurrentPlanError
from teatree.core.models.task_attempt import TaskAttempt
from teatree.core.models.ticket_worktree_checks import dispatch_worktree
from teatree.core.overlay import OverlayBase
from teatree.core.worktree.branch_landed import branch_content_landed_on_base
from teatree.core.worktree.target_branch import bare_target, resolve_target_branch
from teatree.loop.scanners.base import ScanSignal
from teatree.utils.git import git_env_without_overrides
from teatree.utils.run import CommandFailedError, TimeoutExpired, run_bounded_group

if TYPE_CHECKING:
    from teatree.core.models.task import Task
    from teatree.core.models.ticket import Ticket

logger = logging.getLogger(__name__)
_AGE = timedelta(hours=24)
_RECHECK = timedelta(hours=24)


def _remote_branch_state(repo: str, branch: str) -> str:
    try:
        result = run_bounded_group(
            ["git", "-C", repo, "ls-remote", "--exit-code", "origin", f"refs/heads/{bare_target(branch)}"],
            timeout=10,
            expected_codes=None,
            env=git_env_without_overrides(),
        )
    except (CommandFailedError, TimeoutExpired, OSError):
        return "unknown"
    return {0: "present", 2: "missing"}.get(result.returncode, "unknown")


@dataclass(slots=True)
class QueuedTaskFreshnessScanner:
    overlay: OverlayBase
    overlay_name: str
    name: str = "queued_task_freshness"

    def scan(self) -> list[ScanSignal]:
        task_model = cast("type[Task]", apps.get_model("core", "Task"))
        now = timezone.now()
        rows = (
            task_model.objects.filter(
                status=task_model.Status.PENDING,
                ticket__overlay=self.overlay_name,
                created_at__lt=now - _AGE,
            )
            .exclude(ticket__issue_url="")
            .select_related("ticket")
            .order_by("created_at", "pk")[:50]
        )
        signals: list[ScanSignal] = []
        for task in rows:
            if not self._claim(task.ticket, task.pk, now):
                continue
            try:
                finding = self._check(task)
            except Exception:
                logger.exception("queued task %s could not be revalidated", task.pk)
                finding = self._note(task, "recheck failed", changed=False)
            if finding:
                signals.append(
                    ScanSignal(
                        kind="task.premise_changed",
                        summary=f"Task {task.pk}: {finding}",
                        payload={"task_id": task.pk, "ticket_id": task.ticket_id, "reason": finding},
                    )
                )
        return signals

    @staticmethod
    def _claim(ticket: "Ticket", task_id: int, now: datetime) -> bool:
        with transaction.atomic():
            locked = type(ticket).objects.select_for_update().get(pk=ticket.pk)
            stamps = (locked.extra or {}).get("last_validated_at") or {}
            prior = stamps.get(str(task_id)) if isinstance(stamps, dict) else None
            if isinstance(prior, str):
                try:
                    if datetime.fromisoformat(prior) > now - _RECHECK:
                        return False
                except ValueError:
                    pass
            locked.merge_extra(merge_into_dicts={"last_validated_at": {str(task_id): now.isoformat()}})
            return True

    def _check(self, task: "Task") -> str:
        ticket = task.ticket
        host = get_code_host_for_url(self.overlay, ticket.issue_url)
        if host is None:
            return self._note(task, "issue host unavailable", changed=False)
        try:
            issue = host.get_issue(ticket.issue_url)
        except (OSError, RuntimeError, ValueError):
            return self._note(task, "issue could not be read", changed=False)
        if not isinstance(issue, dict) or "error" in issue:
            return self._note(task, "issue could not be read", changed=False)
        if self.overlay.is_issue_done(issue):
            return self._supersede(task, "issue already closed")
        return self._check_worktree(task)

    def _check_worktree(self, task: "Task") -> str:
        ticket = task.ticket
        worktree = dispatch_worktree(ticket)
        if worktree is None:
            return ""
        repo = worktree.worktree_path
        target = resolve_target_branch(ticket, repo, branch=worktree.branch)
        state = _remote_branch_state(repo, target)
        if state == "unknown":
            return self._note(task, "target branch could not be read", changed=False)
        if state == "missing":
            return self._note(task, f"recorded target branch {bare_target(target)} no longer exists")
        if branch_content_landed_on_base(repo, worktree.branch, target):
            return self._supersede(task, "branch content already landed on target")
        try:
            check_plan_current(ticket)
        except NoCurrentPlanError as error:
            reason = self._supersede(task, f"plan no longer current: {error}")
            ticket.schedule_plan_reaffirm(refusal=str(error), parent_task=task)
            return reason
        return ""

    @staticmethod
    def _note(task: "Task", reason: str, *, changed: bool = True) -> str:
        note = f"premise {'changed' if changed else 'unverifiable'}: {reason}"
        if note not in task.execution_reason:
            task.execution_reason = f"{task.execution_reason}\n\n{note}".strip()
            task.save(update_fields=["execution_reason"])
            task.ticket.append_context(f"Queued task {task.pk} {note}")
        return note

    @staticmethod
    def _supersede(task: "Task", reason: str) -> str:
        cause = f"{SUPERSEDED_PREFIX}{reason}"
        with transaction.atomic():
            locked = type(task).objects.select_for_update().get(pk=task.pk)
            if locked.status != locked.Status.PENDING:
                note = f"premise changed after claim: {reason}"
                task.ticket.append_context(f"Queued task {task.pk} {note}")
                return note
            TaskAttempt.objects.create(task=locked, ended_at=timezone.now(), exit_code=1, error=cause)
            locked.fail(reason=cause, by_holder=False)
        task.ticket.append_context(f"Queued task {task.pk} superseded: {reason}")
        return cause
