"""The live-work page — "is anything running, and what" (#3856).

Read-only: no verb on this page mutates anything, so both routes are GET-only. The
body is served as its own fragment so the page can poll itself with the morph-swap
the board uses, keeping scroll position stable across refreshes.
"""

from typing import TYPE_CHECKING

from django.shortcuts import render
from django.views.decorators.http import require_GET

from teatree.dash.live import build_live_view
from teatree.dash.views.access import require_loopback_or_staff
from teatree.dash.views.base import nav_context

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse


@require_loopback_or_staff
@require_GET
def live(request: "HttpRequest") -> "HttpResponse":
    """The full page: running attempts, queue depth, loop liveness, recent outcomes."""
    return render(request, "dash/live.html", {**nav_context("dash:live"), "live": build_live_view()})


@require_loopback_or_staff
@require_GET
def live_body_partial(request: "HttpRequest") -> "HttpResponse":
    """The polled fragment — every panel, so one poll refreshes the whole answer."""
    return render(request, "dash/partials/_live_body.html", {"live": build_live_view()})
