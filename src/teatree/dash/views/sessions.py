"""The agent-session index page — the nav home the transcript viewer lacked (#3873)."""

from typing import TYPE_CHECKING

from django.shortcuts import render
from django.views.decorators.http import require_GET

from teatree.dash.sessions import build_session_index
from teatree.dash.views.access import require_loopback_or_staff
from teatree.dash.views.base import nav_context

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse


@require_loopback_or_staff
@require_GET
def sessions(request: "HttpRequest") -> "HttpResponse":
    """Recent agent sessions, each linking to its redacted transcript tail."""
    context = {**nav_context("dash:sessions"), "sessions": build_session_index()}
    return render(request, "dash/sessions.html", context)
