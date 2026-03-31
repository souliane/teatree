from teatree.agents.services import RuntimeExecution, get_interactive_runtime_name, get_runtime, get_terminal_mode
from teatree.agents.skill_bundle import resolve_skill_bundle
from teatree.core.models import Task
from teatree.core.overlay import SkillMetadata


def run_interactive_task(
    task: Task,
    *,
    phase: str,
    overlay_skill_metadata: SkillMetadata,
) -> RuntimeExecution:
    runtime_name = get_interactive_runtime_name()
    runtime = get_runtime(runtime_name)
    try:
        result = runtime.run(
            task=task,
            skills=resolve_skill_bundle(
                phase=phase,
                overlay_skill_metadata=overlay_skill_metadata,
            ),
            terminal_mode=get_terminal_mode(),
        )
    except Exception as exc:
        task.complete_with_attempt(exit_code=1, error=str(exc))
        raise
    task.complete_with_attempt(artifact_path=result.artifact_path)
    if result.reroute_to == Task.ExecutionTarget.INTERACTIVE:
        task.route_to_interactive(reason="interactive reroute requested")
    return result
