"""Project middleware for the teatree admin dashboard."""

from typing import TYPE_CHECKING

from django.contrib.auth import get_user_model, login

from teatree.config import get_effective_settings

if TYPE_CHECKING:
    from collections.abc import Callable

    from django.http import HttpRequest, HttpResponse

_MODEL_BACKEND = "django.contrib.auth.backends.ModelBackend"
# The operator-observability surfaces the loopback auto-login covers: the Django
# admin and the `teatree.dash` dashboard (#3162). Both ride the same `t3 admin`
# gunicorn process behind the same loopback bind + SSH tunnel, so the same
# auto-login safety boundary applies to both prefixes.
_AUTOLOGIN_PREFIXES = ("/admin/", "/dash/")
_LOOPBACK_IPS = frozenset({"127.0.0.1", "::1"})


class LocalAdminAutoLoginMiddleware:
    """Auto-authenticate the loopback admin dashboard as the superuser.

    Teatree's admin is a single-operator dashboard reached over a loopback bind
    + SSH tunnel (``cli/admin.py`` and the headless deploy), so a login prompt
    is pure friction — a lost password locks the owner out of their own tool. An
    unauthenticated request under one of :data:`_AUTOLOGIN_PREFIXES` (``/admin/``
    or the ``teatree.dash`` dashboard at ``/dash/``, #3162) is logged in as the
    first superuser when BOTH hold:

    * the ``admin_autologin_enabled`` setting is on (DB-home, default on), and
    * the request originates from loopback (``127.0.0.1`` / ``::1``).

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
            request.path.startswith(_AUTOLOGIN_PREFIXES)
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
    """Whether the request's client address is a loopback address.

    Reads ``REMOTE_ADDR`` (the real peer address), never a forwarded header — a
    ``X-Forwarded-For: 127.0.0.1`` from a non-loopback client cannot spoof it.
    The hardcoded loopback set IS the boundary; it is deliberately NOT widened by
    ``settings.INTERNAL_IPS``, so this superuser-auth gate stays decoupled from a
    debug-toolbar knob (a non-loopback IP added there for debugging could never
    widen who is auto-logged-in). A published bridge port NATs the source to the
    docker gateway, so the deploy binds the admin to a real loopback interface
    (host networking) to keep this a genuine ``127.0.0.1`` behind the SSH tunnel.
    """
    return request.META.get("REMOTE_ADDR", "") in _LOOPBACK_IPS
