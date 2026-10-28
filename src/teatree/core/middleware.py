"""Project middleware for the teatree admin dashboard."""

from contextlib import AbstractContextManager, ExitStack
from typing import TYPE_CHECKING, cast

from django.contrib.auth.views import redirect_to_login
from django.db import connections
from django.http import HttpResponse

from teatree import request_cache

if TYPE_CHECKING:
    from collections.abc import Callable

    from django.http import HttpRequest

_PUBLIC_PATHS = frozenset({"/login/", "/admin/login/", "/favicon.ico", "/hooks/gitlab/", "/hooks/github/"})


class OperatorLoginRequiredMiddleware:
    """Require a staff session for dashboard routes, including redirect views."""

    def __init__(self, get_response: "Callable[[HttpRequest], HttpResponse]") -> None:
        self.get_response = get_response

    def __call__(self, request: "HttpRequest") -> "HttpResponse":
        if request.path in _PUBLIC_PATHS or request.path.startswith(("/static/", "/login/local/", "/login/oauth/")):
            return self.get_response(request)
        if request.user.is_authenticated and request.user.is_staff:
            return self.get_response(request)
        destination = redirect_to_login(request.get_full_path(), login_url="/login/")
        if request.headers.get("HX-Request") == "true":
            return HttpResponse(status=401, headers={"HX-Redirect": destination.headers["Location"]})
        return destination


# The statement prefixes that leave the database unchanged. Anything else — an
# INSERT/UPDATE/DELETE, a raw statement, a DDL — drops the memo, so a dashboard POST
# that mutates and then re-renders in the same request reads its own writes.
_READ_ONLY_STATEMENTS = ("SELECT", "PRAGMA", "BEGIN", "SAVEPOINT", "RELEASE", "COMMIT", "ROLLBACK", "EXPLAIN")


class RequestScopedReadCacheMiddleware:
    """Memoize :func:`teatree.request_cache.cached_per_request` reads for one request.

    A dashboard render resolves the effective settings, the active preset and the
    loop table several times over — reads that cannot change while the response is
    being built. Scoping the memo to the request keeps every other entry point (the
    CLI, the loop tick, the test suite) on today's uncached behaviour.

    Correctness comes from the SQL wrapper rather than from listing which views
    mutate: a bulk ``update()`` fires no model signal, so keying invalidation on
    the statement itself is what makes read-your-writes hold for every write path.
    """

    def __init__(self, get_response: "Callable[[HttpRequest], HttpResponse]") -> None:
        self.get_response = get_response

    def __call__(self, request: "HttpRequest") -> "HttpResponse":
        with ExitStack() as stack:
            stack.enter_context(request_cache.request_scope())
            for alias in connections:
                # Django's ``execute_wrapper`` is an unannotated ``@contextmanager``, so
                # its context-manager nature is asserted at this untyped boundary.
                scope = connections[alias].execute_wrapper(_invalidate_on_write)
                stack.enter_context(cast("AbstractContextManager[None]", scope))
            return self.get_response(request)


def _invalidate_on_write(
    execute: "Callable[..., object]",
    sql: str,
    params: object,
    *forwarded: object,
) -> object:
    """Django ``execute_wrapper``: run the statement, then drop the memo if it wrote."""
    result = execute(sql, params, *forwarded)
    if not sql.lstrip().upper().startswith(_READ_ONLY_STATEMENTS):
        request_cache.invalidate()
    return result
