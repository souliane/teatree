"""The "is everything OK?" health view and its htmx-poll bands partial (#3162)."""

from typing import TYPE_CHECKING, TypedDict

from django.shortcuts import render
from django.views.decorators.http import require_GET

from teatree.dash.commands import command_buttons
from teatree.dash.health_bands import HealthView, build_health_view
from teatree.dash.views.access import require_loopback_or_staff
from teatree.dash.views.base import nav_context

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse


class HealthContext(TypedDict):
    health: HealthView


def _health_context() -> HealthContext:
    return {"health": build_health_view()}


@require_loopback_or_staff
@require_GET
def health(request: "HttpRequest") -> "HttpResponse":
    """Full health page — verdict / loops / capacity / mode bands + command buttons."""
    context = {**nav_context("dash:health"), **_health_context(), "command_buttons": command_buttons()}
    return render(request, "dash/health.html", context)


@require_loopback_or_staff
@require_GET
def health_bands_partial(request: "HttpRequest") -> "HttpResponse":
    """The four bands fragment — the target of the htmx poll."""
    return render(request, "dash/partials/_health_bands.html", _health_context())
