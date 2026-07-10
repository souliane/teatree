"""Project middleware for the teatree admin dashboard."""

from typing import TYPE_CHECKING

from django.conf import settings
from django.contrib.auth import get_user_model, login

from teatree.config import get_effective_settings

if TYPE_CHECKING:
    from collections.abc import Callable

    from django.http import HttpRequest, HttpResponse

_MODEL_BACKEND = "django.contrib.auth.backends.ModelBackend"
_ADMIN_PREFIX = "/admin/"
_LOOPBACK_IPS = frozenset({"127.0.0.1", "::1"})


class LocalAdminAutoLoginMiddleware:
    """Auto-authenticate the loopback admin dashboard as the superuser.

    Teatree's admin is a single-operator dashboard reached over a loopback bind
    + SSH tunnel (``cli/admin.py`` and the headless deploy), so a login prompt
    is pure friction — a lost password locks the owner out of their own tool. An
    unauthenticated ``/admin/`` request is logged in as the first superuser when
    BOTH hold:

    * the ``admin_autologin_enabled`` setting is on (DB-home, default on), and
    * the request originates from loopback (``127.0.0.1`` / ``::1`` / ``INTERNAL_IPS``).

    The loopback check is the hard security boundary — auto-login NEVER fires for
    a non-loopback request, even with the flag on — so a non-loopback deployment
    of the admin cannot silently open it. This is deliberately decoupled from
    ``DEBUG``: the admin now mounts and serves independent of ``DEBUG``, so the
    old ``DEBUG`` gate would have been meaningless. Place this after
    ``AuthenticationMiddleware`` so ``request.user`` is already resolved.
    """

    def __init__(self, get_response: "Callable[[HttpRequest], HttpResponse]") -> None:
        self.get_response = get_response

    def __call__(self, request: "HttpRequest") -> "HttpResponse":
        if (
            request.path.startswith(_ADMIN_PREFIX)
            and not request.user.is_authenticated
            and _request_is_loopback(request)
            and get_effective_settings().admin_autologin_enabled
        ):
            superuser = get_user_model().objects.filter(is_superuser=True).first()
            if superuser is not None:
                superuser.backend = _MODEL_BACKEND
                login(request, superuser)
        return self.get_response(request)


def _request_is_loopback(request: "HttpRequest") -> bool:
    """Whether the request's client address is a loopback / internal address.

    A published bridge port NATs the source to the docker gateway, so the deploy
    binds the admin to a real loopback interface (host networking) to keep this a
    genuine ``127.0.0.1`` behind the SSH tunnel.
    """
    remote_addr = request.META.get("REMOTE_ADDR", "")
    return remote_addr in _LOOPBACK_IPS or remote_addr in set(settings.INTERNAL_IPS)
