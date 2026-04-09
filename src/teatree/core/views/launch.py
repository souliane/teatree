import logging
import shutil

from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from teatree.core.models import InvalidTransitionError, Task
from teatree.core.overlay_loader import get_overlay
from teatree.types import SkillMetadata

logger = logging.getLogger(__name__)


@method_decorator(csrf_exempt, name="dispatch")
class LaunchAgentView(View):
    def post(self, request: HttpRequest, task_id: int) -> HttpResponse:
        try:
            task = Task.objects.get(pk=task_id)
        except Task.DoesNotExist:
            raise Http404 from None

        try:
            task.claim(claimed_by=f"launch-{request.user}")
        except InvalidTransitionError as exc:
            return JsonResponse({"error": str(exc)}, status=409)

        try:
            overlay = get_overlay()
            skill_metadata = overlay.metadata.get_skill_metadata()
            if task.execution_target == Task.ExecutionTarget.INTERACTIVE:
                terminal_mode = request.POST.get("terminal_mode", "")
                terminal_app = request.POST.get("terminal_app", "")
                return self._launch_interactive(
                    task,
                    skill_metadata,
                    terminal_mode=terminal_mode,
                    terminal_app=terminal_app,
                )
            return self._launch_headless(task)
        except Exception as exc:
            logger.exception("Launch failed for task %s", task_id)
            error_msg = str(exc)
            task.complete_with_attempt(exit_code=1, error=error_msg)
            return JsonResponse({"error": error_msg}, status=500)

    def _launch_interactive(
        self,
        task: Task,
        skill_metadata: SkillMetadata,
        *,
        terminal_mode: str = "",
        terminal_app: str = "",
    ) -> JsonResponse:
        return launch_interactive_task(
            task,
            skill_metadata,
            terminal_mode=terminal_mode,
            terminal_app=terminal_app,
        )

    def _launch_headless(self, task: Task) -> JsonResponse:
        from teatree.core.tasks import execute_headless_task  # noqa: PLC0415

        django_task_result = execute_headless_task.enqueue(int(task.pk), task.phase)
        return JsonResponse(
            {
                "status": "queued",
                "django_task_id": str(django_task_result.id),
            },
        )


def launch_interactive_task(
    task: Task,
    skill_metadata: SkillMetadata,
    *,
    terminal_mode: str = "",
    terminal_app: str = "",
) -> JsonResponse:
    from teatree.agents.web_terminal import launch_web_session  # noqa: PLC0415

    attempt = launch_web_session(
        task,
        phase=task.phase,
        overlay_skill_metadata=skill_metadata,
        terminal_mode=terminal_mode,
        terminal_app=terminal_app,
    )
    return JsonResponse({"launch_url": attempt.launch_url, "attempt_id": attempt.pk})


@method_decorator(csrf_exempt, name="dispatch")
class LaunchTerminalView(View):
    def post(self, request: HttpRequest) -> HttpResponse:
        import os  # noqa: PLC0415

        from teatree.agents.services import get_terminal_mode  # noqa: PLC0415
        from teatree.agents.terminal_launcher import launch as terminal_launch  # noqa: PLC0415

        mode = request.POST.get("terminal_mode") or get_terminal_mode()
        app = request.POST.get("terminal_app", "")
        shell = os.environ.get("SHELL", "/bin/zsh")
        result = terminal_launch([shell, "-l"], mode=mode, app=app)

        if result.launch_url:
            return JsonResponse({"launch_url": result.launch_url})
        return JsonResponse({"launched": True, "mode": result.mode})


def launch_interactive_for_task(task: "Task") -> str:
    """Launch an interactive agent session for a task and return the launch URL."""
    from teatree.agents.services import get_terminal_mode  # noqa: PLC0415
    from teatree.agents.terminal_launcher import launch as terminal_launch  # noqa: PLC0415

    claude_bin = shutil.which("claude")
    if not claude_bin:
        return ""

    result = terminal_launch([claude_bin], mode=get_terminal_mode())
    logger.info("Launched interactive session for task %s (mode=%s)", task.pk, result.mode)
    return result.launch_url


@method_decorator(csrf_exempt, name="dispatch")
class LaunchInteractiveAgentView(View):
    def post(self, request: HttpRequest) -> HttpResponse:
        from teatree.agents.services import get_terminal_mode  # noqa: PLC0415
        from teatree.agents.terminal_launcher import launch as terminal_launch  # noqa: PLC0415

        claude_bin = shutil.which("claude")
        if claude_bin is None:
            return JsonResponse({"error": "claude CLI not found on PATH"}, status=500)

        mode = request.POST.get("terminal_mode") or get_terminal_mode()
        app = request.POST.get("terminal_app", "")
        result = terminal_launch([claude_bin], mode=mode, app=app)

        if result.launch_url:
            return JsonResponse({"launch_url": result.launch_url})
        return JsonResponse({"launched": True, "mode": result.mode})
