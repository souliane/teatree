from pathlib import Path

from teatree.agents.services import RuntimeExecution, get_headless_runtime_name, get_runtime
from teatree.agents.skill_bundle import DEFAULT_DELEGATION_MAP, resolve_skill_bundle
from teatree.core.models import Task
from teatree.core.overlay import SkillMetadata


def run_headless_task(
    task: Task,
    *,
    phase: str,
    overlay_skill_metadata: SkillMetadata,
    delegation_map_path: Path = DEFAULT_DELEGATION_MAP,
) -> RuntimeExecution:
    runtime_name = get_headless_runtime_name()
    runtime = get_runtime(runtime_name)
    try:
        result = runtime.run(
            task=task,
            skills=resolve_skill_bundle(
                phase=phase,
                overlay_skill_metadata=overlay_skill_metadata,
                delegation_map_path=delegation_map_path,
            ),
        )
    except Exception as exc:
        task.complete_with_attempt(exit_code=1, error=str(exc))
        raise
    task.complete_with_attempt(artifact_path=result.artifact_path)
    return result
