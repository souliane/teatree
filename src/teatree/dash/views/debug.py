"""Debug access: the loopback ttyd "Debug session" button + allowlisted command buttons (#3162).

Both POST-only, CSRF-protected, and audited. Neither exposes anything beyond
loopback: the ttyd terminal binds ``127.0.0.1`` reached through the same SSH
tunnel, and the command buttons run a fixed code allowlist as bounded subprocesses.
"""

from typing import TYPE_CHECKING

from django.http import HttpResponseBadRequest
from django.shortcuts import render
from django.views.decorators.http import require_POST

from teatree.agents.web_terminal import launch_web_session
from teatree.dash import audit
from teatree.dash.commands import CommandNotAllowedError, run_allowlisted
from teatree.dash.views.access import require_loopback_or_staff
from teatree.dash.views.base import actor

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse


@require_loopback_or_staff
@require_POST
def debug_session(request: "HttpRequest") -> "HttpResponse":
    """Spawn a loopback ttyd terminal wrapping a fresh or resumed ``claude`` session."""
    resume = request.POST.get("resume_session_id", "").strip()
    try:
        result = launch_web_session(resume)
    except (FileNotFoundError, ValueError) as exc:
        return HttpResponseBadRequest(str(exc))
    audit.record(actor=actor(request), action="debug:session", target=resume, after=result.launch_url or result.error)
    return render(request, "dash/partials/_debug_session.html", {"result": result})


@require_loopback_or_staff
@require_POST
def command_run(request: "HttpRequest") -> "HttpResponse":
    """Run one allowlisted ``t3`` command as a bounded subprocess and show its output."""
    key = request.POST.get("command", "").strip()
    loop_name = request.POST.get("loop", "").strip()
    try:
        result = run_allowlisted(key, loop_name=loop_name)
    except CommandNotAllowedError as exc:
        return HttpResponseBadRequest(str(exc))
    audit.record(actor=actor(request), action=f"command:{key}", target=loop_name, after=f"exit={result.exit_code}")
    return render(request, "dash/partials/_command_result.html", {"result": result})
