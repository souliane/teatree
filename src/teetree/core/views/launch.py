import logging
import shutil
import subprocess  # noqa: S404

from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from teetree.core.models import InvalidTransitionError, Task
from teetree.core.overlay import SkillMetadata
from teetree.core.overlay_loader import get_overlay

logger = logging.getLogger(__name__)


def _find_free_port() -> int:
    from teetree.utils.ports import find_free_port  # noqa: PLC0415

    return find_free_port()


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
            skill_metadata = overlay.get_skill_metadata()
            if task.execution_target == Task.ExecutionTarget.INTERACTIVE:
                return self._launch_interactive(task, skill_metadata)
            return self._launch_headless(task)
        except Exception as exc:
            logger.exception("Launch failed for task %s", task_id)
            error_msg = str(exc)
            task.complete_with_attempt(exit_code=1, error=error_msg)
            return JsonResponse({"error": error_msg}, status=500)

    def _launch_interactive(self, task: Task, skill_metadata: SkillMetadata) -> JsonResponse:
        from teetree.agents.web_terminal import launch_web_session  # noqa: PLC0415

        attempt = launch_web_session(
            task,
            phase=task.phase,
            overlay_skill_metadata=skill_metadata,
        )
        return JsonResponse({"launch_url": attempt.launch_url, "attempt_id": attempt.pk})

    def _launch_headless(self, task: Task) -> JsonResponse:
        from teetree.core.tasks import execute_headless_task  # noqa: PLC0415

        django_task_result = execute_headless_task.enqueue(int(task.pk), task.phase)
        return JsonResponse(
            {
                "status": "queued",
                "django_task_id": str(django_task_result.id),
            }
        )


@method_decorator(csrf_exempt, name="dispatch")
class LaunchTerminalView(View):
    def post(self, _request: HttpRequest) -> HttpResponse:
        import os  # noqa: PLC0415

        ttyd_bin = shutil.which("ttyd")
        if ttyd_bin is None:
            return JsonResponse({"error": "ttyd not installed (brew install ttyd)"}, status=500)

        shell = os.environ.get("SHELL", "/bin/zsh")
        port = _find_free_port()

        subprocess.Popen(  # noqa: S603
            [ttyd_bin, "--writable", "--port", str(port), "--once", shell, "-l"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

        launch_url = f"http://127.0.0.1:{port}"
        logger.info("Launched terminal ttyd (port=%s, shell=%s)", port, shell)
        return JsonResponse({"launch_url": launch_url})


def launch_interactive_for_task(task: "Task") -> str:
    """Launch a ttyd+claude session for a task and return the launch URL."""
    claude_bin = shutil.which("claude")
    ttyd_bin = shutil.which("ttyd")
    if not claude_bin or not ttyd_bin:
        return ""

    port = _find_free_port()
    subprocess.Popen(  # noqa: S603
        [ttyd_bin, "--writable", "--port", str(port), "--once", claude_bin],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )

    launch_url = f"http://127.0.0.1:{port}"
    logger.info("Launched interactive session for task %s (port=%s)", task.pk, port)
    return launch_url


@method_decorator(csrf_exempt, name="dispatch")
class LaunchInteractiveAgentView(View):
    def post(self, _request: HttpRequest) -> HttpResponse:
        claude_bin = shutil.which("claude")
        if claude_bin is None:
            return JsonResponse({"error": "claude CLI not found on PATH"}, status=500)

        ttyd_bin = shutil.which("ttyd")
        if ttyd_bin is None:
            return JsonResponse({"error": "ttyd not installed (brew install ttyd)"}, status=500)

        port = _find_free_port()

        subprocess.Popen(  # noqa: S603
            [ttyd_bin, "--writable", "--port", str(port), "--once", claude_bin],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

        launch_url = f"http://127.0.0.1:{port}"
        logger.info("Launched interactive agent ttyd (port=%s)", port)
        return JsonResponse({"launch_url": launch_url})
