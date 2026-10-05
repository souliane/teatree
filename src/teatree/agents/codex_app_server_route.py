"""Runtime-only main-clone check for Codex worker routing."""

from teatree.agents._runner_options import _resolve_task_cwd
from teatree.core.gates.main_clone_env import is_managed_main_clone
from teatree.core.models import Task


def starts_in_managed_main_clone(task: Task) -> bool:
    cwd = _resolve_task_cwd(task)
    return bool(cwd and is_managed_main_clone(cwd))
