import os
import subprocess  # noqa: S404
from pathlib import Path

from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from django.template.response import TemplateResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from django_fsm import TransitionNotAllowed

from teatree.core.models import Session, Task, Ticket
from teatree.core.views._startup import perform_sync


@method_decorator(csrf_exempt, name="dispatch")
class CancelTaskView(View):
    def post(self, request: HttpRequest, task_id: int) -> HttpResponse:
        from django.db import transaction  # noqa: PLC0415

        try:
            with transaction.atomic():
                task = Task.objects.select_for_update().get(pk=task_id)
                if task.status in {Task.Status.COMPLETED, Task.Status.FAILED}:
                    return JsonResponse({"error": "Task already finished"}, status=409)
                if task.status == Task.Status.CLAIMED and request.POST.get("confirm") != "true":
                    return JsonResponse(
                        {"error": "Task is in progress. Pass confirm=true to cancel."},
                        status=409,
                    )
                task.fail()
        except Task.DoesNotExist:
            raise Http404 from None

        return JsonResponse({"task_id": task.pk, "status": task.status})


@method_decorator(csrf_exempt, name="dispatch")
class SyncFollowupView(View):
    def post(self, _request: HttpRequest) -> HttpResponse:
        result = perform_sync()
        return TemplateResponse(
            _request,
            "teatree/partials/sync_result.html",
            {"result": result},
        )


_ALLOWED_TRANSITIONS = {
    "scope",
    "start",
    "code",
    "test",
    "review",
    "ship",
    "request_review",
    "mark_merged",
    "mark_delivered",
    "rework",
}


@method_decorator(csrf_exempt, name="dispatch")
class TicketTransitionView(View):
    def post(self, request: HttpRequest, ticket_id: int) -> HttpResponse:
        transition_name = request.POST.get("transition", "")
        if transition_name not in _ALLOWED_TRANSITIONS:
            return JsonResponse({"error": f"Unknown transition: {transition_name}"}, status=400)

        try:
            ticket = Ticket.objects.get(pk=ticket_id)
        except Ticket.DoesNotExist:
            raise Http404 from None

        method = getattr(ticket, transition_name, None)
        if method is None:
            return JsonResponse({"error": f"Invalid transition: {transition_name}"}, status=400)

        try:
            method()
            ticket.save()
        except TransitionNotAllowed:
            return JsonResponse(
                {"error": f"Transition '{transition_name}' not allowed from state '{ticket.get_state_display()}'"},
                status=409,
            )

        return JsonResponse({"ticket_id": ticket.pk, "state": ticket.get_state_display()})


@method_decorator(csrf_exempt, name="dispatch")
class CreateTaskView(View):
    def post(self, request: HttpRequest, ticket_id: int) -> HttpResponse:
        try:
            ticket = Ticket.objects.get(pk=ticket_id)
        except Ticket.DoesNotExist:
            raise Http404 from None

        phase = request.POST.get("phase", "coding")
        target = request.POST.get("target", Task.ExecutionTarget.HEADLESS)

        session = Session.objects.filter(ticket=ticket).order_by("-pk").first()
        if session is None:
            session = Session.objects.create(ticket=ticket, agent_id="dashboard")

        task = Task.objects.create(
            ticket=ticket,
            session=session,
            phase=phase,
            execution_target=target,
            execution_reason=f"Started from dashboard ({phase})",
        )

        if target == Task.ExecutionTarget.HEADLESS:
            from teatree.core.tasks import execute_headless_task  # noqa: PLC0415

            try:
                execute_headless_task.enqueue(int(task.pk), phase)
            except Exception as exc:  # noqa: BLE001
                task.complete_with_attempt(exit_code=1, error=str(exc))
                return JsonResponse({"error": str(exc)}, status=500)

            return JsonResponse({"task_id": task.pk, "status": task.status})

        return JsonResponse({"task_id": task.pk, "status": task.status})


def _get_t3_repo() -> Path | None:
    """Resolve the teatree repo root from T3_REPO env var or package location."""
    env_path = os.environ.get("T3_REPO", "")
    if env_path:
        return Path(env_path).expanduser()
    # Fall back to auto-detect from package location
    pkg_root = Path(__file__).resolve().parents[4]
    if (pkg_root / ".git").is_dir():
        return pkg_root
    return None


@method_decorator(csrf_exempt, name="dispatch")
class GitPullView(View):
    def post(self, _request: HttpRequest) -> HttpResponse:
        t3_repo = _get_t3_repo()
        if t3_repo is None or not t3_repo.is_dir():
            return JsonResponse({"error": "T3_REPO not found"}, status=400)

        try:
            result = subprocess.run(
                ["git", "pull"],  # noqa: S607
                cwd=t3_repo,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return JsonResponse({"error": "git pull timed out after 30s"}, status=500)

        if result.returncode != 0:
            error = (result.stderr or result.stdout).strip()
            self._create_interactive_task(error, t3_repo)
            return JsonResponse({"error": error, "task_created": True}, status=500)

        output = result.stdout.strip()
        return JsonResponse({"ok": True, "output": output})

    @staticmethod
    def _create_interactive_task(error: str, t3_repo: Path) -> None:
        ticket, _created = Ticket.objects.get_or_create(
            overlay="teatree",
            issue_url="",
            defaults={"state": Ticket.State.STARTED},
        )
        session = Session.objects.create(ticket=ticket, agent_id="dashboard")
        Task.objects.create(
            ticket=ticket,
            session=session,
            phase="maintenance",
            execution_target=Task.ExecutionTarget.INTERACTIVE,
            execution_reason=f"git pull failed in {t3_repo}:\n{error}",
        )
