"""Project middleware for the local teatree dev server."""

from typing import TYPE_CHECKING

from django.conf import settings
from django.contrib.auth import get_user_model, login

if TYPE_CHECKING:
    from collections.abc import Callable

    from django.http import HttpRequest, HttpResponse

_MODEL_BACKEND = "django.contrib.auth.backends.ModelBackend"
_ADMIN_PREFIX = "/admin/"


class LocalAdminAutoLoginMiddleware:
    """Auto-authenticate the local admin dashboard as the superuser.

    Teatree's admin is a single-user dashboard bound to ``127.0.0.1`` (see
    ``cli/admin.py``), so a login prompt is pure friction — a lost password
    locks the owner out of their own local tool. When ``DEBUG`` is on, an
    unauthenticated ``/admin/`` request is logged in as the first superuser.

    The only guard is ``settings.DEBUG``: if teatree is ever run with ``DEBUG``
    off, this middleware is inert and Django's normal auth wall returns. Auth is
    never disabled outside local dev. Place this after
    ``AuthenticationMiddleware`` so ``request.user`` is already resolved.
    """

    def __init__(self, get_response: "Callable[[HttpRequest], HttpResponse]") -> None:
        self.get_response = get_response

    def __call__(self, request: "HttpRequest") -> "HttpResponse":
        if settings.DEBUG and request.path.startswith(_ADMIN_PREFIX) and not request.user.is_authenticated:
            superuser = get_user_model().objects.filter(is_superuser=True).first()
            if superuser is not None:
                superuser.backend = _MODEL_BACKEND
                login(request, superuser)
        return self.get_response(request)
