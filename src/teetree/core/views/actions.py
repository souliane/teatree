import json
import os
from pathlib import Path

from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from django.template.response import TemplateResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from django_fsm import TransitionNotAllowed

from teetree.core.models import InvalidTransitionError, Session, Task, Ticket
from teetree.core.views._startup import perform_sync


@method_decorator(csrf_exempt, name="dispatch")
class CancelTaskView(View):
    def post(self, request: HttpRequest, task_id: int) -> HttpResponse:
        from django.db import transaction  # noqa: PLC0415

        force = request.POST.get("force") == "1"

        try:
            with transaction.atomic():
                task = Task.objects.select_for_update().get(pk=task_id)
                if task.status in {Task.Status.COMPLETED, Task.Status.FAILED}:
                    return JsonResponse({"error": "Task already finished"}, status=409)
                if (
                    not force
                    and task.status == Task.Status.CLAIMED
                    and task.execution_target == Task.ExecutionTarget.INTERACTIVE
                    and _has_active_session(task)
                ):
                    return JsonResponse(
                        {
                            "error": "This task has an active interactive session. "
                            "Close the terminal to end it, or pass force=1 to override."
                        },
                        status=409,
                    )
                task.fail()
        except Task.DoesNotExist:
            raise Http404 from None

        return JsonResponse({"task_id": task.pk, "status": task.status})


def _has_active_session(task: Task) -> bool:
    attempt = task.attempts.order_by("-pk").first()
    if attempt is None or not attempt.agent_session_id:
        return False
    session_file = Path.home() / ".claude" / "sessions" / f"{attempt.agent_session_id}.json"
    if not session_file.is_file():
        return False
    try:
        data = json.loads(session_file.read_text(encoding="utf-8"))
        pid = data.get("pid")
        if isinstance(pid, int):
            os.kill(pid, 0)
            return True
    except (json.JSONDecodeError, OSError):
        pass
    return False


@method_decorator(csrf_exempt, name="dispatch")
class SyncFollowupView(View):
    def post(self, _request: HttpRequest) -> HttpResponse:
        result = perform_sync()
        return TemplateResponse(
            _request,
            "teetree/partials/sync_result.html",
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
            from teetree.core.tasks import execute_headless_task  # noqa: PLC0415

            try:
                task.claim(claimed_by="auto-launch")
                execute_headless_task.enqueue(int(task.pk), phase)
            except InvalidTransitionError as exc:
                return JsonResponse({"error": str(exc)}, status=409)
            except Exception as exc:  # noqa: BLE001
                task.complete_with_attempt(exit_code=1, error=str(exc))
                return JsonResponse({"error": str(exc)}, status=500)

            return JsonResponse({"task_id": task.pk, "status": task.status})

        return JsonResponse({"task_id": task.pk, "status": task.status})
