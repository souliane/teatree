"""The "what is this box configured to do?" page and its htmx-poll partial (#3664)."""

from typing import TYPE_CHECKING, TypedDict

from django.shortcuts import render
from django.views.decorators.http import require_GET

from teatree.dash.config_surface import ConfigView, build_config_view
from teatree.dash.views.access import require_loopback_or_staff
from teatree.dash.views.base import nav_context

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse


class ConfigContext(TypedDict):
    config: ConfigView


def _config_context() -> ConfigContext:
    return {"config": build_config_view()}


@require_loopback_or_staff
@require_GET
def config(request: "HttpRequest") -> "HttpResponse":
    """Full configuration page — model / credentials / kill switches / limits / self-repairs."""
    return render(request, "dash/config.html", {**nav_context("dash:config"), **_config_context()})


@require_loopback_or_staff
@require_GET
def config_bands_partial(request: "HttpRequest") -> "HttpResponse":
    """The configuration bands fragment — the target of the htmx poll."""
    return render(request, "dash/partials/_config_bands.html", _config_context())
