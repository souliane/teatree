"""Opt-in redacted agent-transcript click-through (#3673 Tier 2).

Reached only from an explicit link in the ticket drawer — never during list
rendering. Loopback/staff-gated like every other dash view; the tail read is
bounded and each line is already redacted by :func:`tail_transcript` before it
reaches the template.
"""

from typing import TYPE_CHECKING

from django.shortcuts import render
from django.views.decorators.http import require_GET

from teatree.dash.transcript import tail_transcript
from teatree.dash.views.access import require_loopback_or_staff

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse


@require_loopback_or_staff
@require_GET
def transcript(request: "HttpRequest", session_id: str) -> "HttpResponse":
    """Render the redacted tail of one agent session's transcript."""
    entries = tail_transcript(session_id)
    return render(
        request,
        "dash/partials/_transcript.html",
        {"session_id": session_id, "entries": entries},
    )
