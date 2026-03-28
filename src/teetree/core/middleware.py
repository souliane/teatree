from django.conf import settings
from django.http import HttpRequest, HttpResponseForbidden

_LOCALHOST_ADDRS = {"127.0.0.1", "::1", "localhost"}


class LocalOnlyMiddleware:
    def __init__(self, get_response):  # noqa: ANN001
        self.get_response = get_response
        self.allowed = set(
            getattr(settings, "TEATREE_DASHBOARD_ALLOWED_HOSTS", _LOCALHOST_ADDRS)
        )

    def __call__(self, request: HttpRequest):  # noqa: ANN204
        if request.method == "POST":
            remote = request.META.get("REMOTE_ADDR", "")
            if remote not in self.allowed:
                return HttpResponseForbidden("Dashboard POST restricted to local access.")
        return self.get_response(request)
