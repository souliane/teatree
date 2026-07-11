"""Defense-in-depth access gate for the dashboard control + mutation views (#3164).

The dashboard's security rests on the gunicorn loopback bind plus the
loopback-only auto-login middleware. This decorator adds a second, view-level
assertion so a ``--host 0.0.0.0`` misconfiguration cannot expose loop-control
mutations, the danger-gate toggle, FSM transitions, or command output to an
anonymous off-loopback caller: a request that is neither from loopback nor from
an authenticated staff user is refused with ``403``.
"""

from functools import wraps
from typing import TYPE_CHECKING

from django.http import HttpResponseForbidden

from teatree.core.middleware import request_is_loopback

if TYPE_CHECKING:
    from collections.abc import Callable

    from django.http import HttpRequest, HttpResponse


def require_loopback_or_staff(view: "Callable[..., HttpResponse]") -> "Callable[..., HttpResponse]":
    """Refuse a dashboard request that is neither from loopback nor an authenticated staff user."""

    @wraps(view)
    def _guarded(request: "HttpRequest", *args: object, **kwargs: object) -> "HttpResponse":
        user = getattr(request, "user", None)
        is_staff = user is not None and user.is_authenticated and user.is_staff
        if request_is_loopback(request) or is_staff:
            return view(request, *args, **kwargs)
        return HttpResponseForbidden("dashboard is loopback-only")

    return _guarded
