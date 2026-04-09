from collections.abc import Callable

from django.http import HttpRequest, HttpResponse, JsonResponse

_LOCAL_ADDRS = {"127.0.0.1", "::1"}


class LocalhostOnlyMiddleware:
    """Reject non-localhost requests to mutating endpoints with 403.

    GET/HEAD/OPTIONS/TRACE are allowed from any source (read-only).
    All other methods (POST, PUT, DELETE, PATCH) are restricted to
    requests originating from localhost.

    This is a defence-in-depth layer for the local dashboard — the
    primary protection is that the server only binds to 127.0.0.1.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if request.method not in {"GET", "HEAD", "OPTIONS", "TRACE"}:
            remote = request.META.get("REMOTE_ADDR", "")
            if remote not in _LOCAL_ADDRS:
                return JsonResponse(
                    {"error": "Dashboard actions are restricted to localhost"},
                    status=403,
                )
        return self.get_response(request)
