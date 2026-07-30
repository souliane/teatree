"""Opt-in redacted agent-transcript click-through (#3673 Tier 2).

Reached only from an explicit link in the ticket drawer — never during list
rendering. Loopback/staff-gated like every other dash view; the tail read is
bounded and each line is already redacted by :func:`tail_transcript` before it
reaches the template.

The drawer links this with a real ``href``, so the route is reachable by an ordinary
navigation (new tab, middle-click, JavaScript off) as well as by htmx. It answers the
bare fragment only to an ``HX-Request``; a plain GET gets the full page, since a
fragment served as a document is an unstyled dead end with no nav and no way back.
"""

from typing import TYPE_CHECKING

from django.shortcuts import render
from django.views.decorators.http import require_GET

from teatree.dash.transcript import tail_transcript
from teatree.dash.views.access import require_loopback_or_staff
from teatree.dash.views.base import nav_context

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse


@require_loopback_or_staff
@require_GET
def transcript(request: "HttpRequest", session_id: str) -> "HttpResponse":
    """Render the redacted tail of one agent session's transcript."""
    context = {"session_id": session_id, "entries": tail_transcript(session_id)}
    if request.headers.get("HX-Request") == "true":
        return render(request, "dash/partials/_transcript.html", context)
    return render(request, "dash/transcript.html", {**nav_context("dash:sessions"), **context})
