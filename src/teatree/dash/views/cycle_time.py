"""The cycle-time page — where a ticket's time goes, and whether that is getting worse (#3847)."""

from typing import TYPE_CHECKING

from django.shortcuts import render
from django.views.decorators.http import require_GET

from teatree.dash.cycle_time import DEFAULT_WINDOW_DAYS, build_cycle_time_view, clamp_window_days
from teatree.dash.views.access import require_loopback_or_staff
from teatree.dash.views.base import nav_context

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse


@require_loopback_or_staff
@require_GET
def cycle_time(request: "HttpRequest") -> "HttpResponse":
    """Aggregate distribution, per-transition trend, and one stacked bar per ticket."""
    window_days = clamp_window_days(request.GET.get("days", str(DEFAULT_WINDOW_DAYS)))
    context = {**nav_context("dash:cycle_time"), "view": build_cycle_time_view(window_days=window_days)}
    return render(request, "dash/cycle_time.html", context)
