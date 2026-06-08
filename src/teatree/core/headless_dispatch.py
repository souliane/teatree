"""Registry for the headless task runner — the core → agents inversion seam (#1922).

``core.tasks.execute_headless_task`` (a django-tasks worker) must run a task in a
headless ``claude -p`` subprocess, but that runner lives in ``teatree.agents``
(the higher layer). Rather than ``core`` importing ``agents``, ``agents``
registers its runner here at app-ready time and ``core`` resolves it through this
registry.

Fail-LOUD: a missing runner is fatal (a dispatched headless task that silently
does nothing is worse than a clear error), so :func:`get_headless_runner` raises
when nothing is registered.
"""

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from teatree.core.models import Task, TaskAttempt
    from teatree.types import SkillMetadata


class HeadlessRunner(Protocol):
    def __call__(
        self,
        task: "Task",
        *,
        phase: str,
        overlay_skill_metadata: "SkillMetadata",
    ) -> "TaskAttempt": ...  # pragma: no branch


_runner: HeadlessRunner | None = None


def register_headless_runner(runner: HeadlessRunner) -> None:
    global _runner  # noqa: PLW0603 — single process-wide runner registered at app-ready
    _runner = runner


def get_headless_runner() -> HeadlessRunner:
    if _runner is None:
        msg = (
            "no headless runner registered — teatree.agents.apps.AgentsConfig.ready() "
            "must run before a headless task is dispatched"
        )
        raise RuntimeError(msg)
    return _runner


def loop_dispatch_refusal(task: "Task") -> str | None:
    """Reason a headless dispatch of ``task`` is refused, or ``None`` to proceed.

    The single fail-closed billing guard both headless entry points consult
    (souliane/teatree#1375): a loop-dispatched phase task — one whose
    ``(ticket.role, phase)`` has a registered phase agent (``Task.loop_dispatched``)
    — must run INTERACTIVE in the in-session ``/loop`` slot, never as a metered
    detached ``claude -p`` subprocess (post-2026-06-15 billing). Unless the single
    ``LOOP_ALLOW_HEADLESS_DISPATCH`` toggle is explicitly enabled, return a
    ``routing_error`` reason so the caller records a refusal instead of shelling
    out. Free-form headless work (no registered phase agent) returns ``None`` and
    proceeds.

    Both ``core.tasks.execute_headless_task`` (the django-tasks worker) and
    ``core.management.commands.tasks.Command._execute_sdk`` (the ``work-next-sdk``
    CLI path) call this so the guard cannot drift between the two seams.
    """
    from django.conf import settings  # noqa: PLC0415

    from teatree.core.models import Task  # noqa: PLC0415

    if getattr(settings, "LOOP_ALLOW_HEADLESS_DISPATCH", False):
        return None
    if not Task.loop_dispatched(role=task.ticket.role, phase=task.phase):
        return None
    return (
        f"refused headless dispatch for loop-dispatched phase "
        f"(role={task.ticket.role!r}, phase={task.phase!r}): "
        "this task must run INTERACTIVE via the /loop slot "
        "(set LOOP_ALLOW_HEADLESS_DISPATCH=True to override)"
    )
