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
