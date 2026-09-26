"""Registry for the task runner — the core → agents inversion seam (#1922).

``core.tasks.execute_task`` (a django-tasks worker) must run a task in a detached
agent run, but that runner lives in ``teatree.agents`` (the higher layer). Rather
than ``core`` importing ``agents``, ``agents`` registers its runner here at
app-ready time and ``core`` resolves it through this registry.

Fail-LOUD: a missing runner is fatal (a dispatched task that silently does
nothing is worse than a clear error), so :func:`get_agent_runner` raises when
nothing is registered.
"""

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from teatree.core.models import Task, TaskAttempt
    from teatree.types import SkillMetadata


class AgentRunner(Protocol):
    def __call__(
        self,
        task: "Task",
        *,
        phase: str,
        overlay_skill_metadata: "SkillMetadata",
    ) -> "TaskAttempt": ...  # pragma: no branch


_runner: AgentRunner | None = None


def register_agent_runner(runner: AgentRunner) -> None:
    global _runner  # noqa: PLW0603 — single process-wide runner registered at app-ready
    _runner = runner


def get_agent_runner() -> AgentRunner:
    if _runner is None:
        msg = (
            "no agent runner registered — teatree.agents.apps.AgentsConfig.ready() must run before a task is dispatched"
        )
        raise RuntimeError(msg)
    return _runner
