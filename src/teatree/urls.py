from django.contrib import admin
from django.contrib.staticfiles.views import serve as serve_static
from django.urls import include, path, re_path

urlpatterns = [
    path("", include("teatree.core.urls", namespace="teatree")),
    # The first-party admin dashboard (#3162) — ticket-FSM kanban, health, and
    # loop control. Rides this same gunicorn process on the same loopback port,
    # behind the same loopback auto-login (its prefix gate covers `/dash/`).
    path("dash/", include("teatree.dash.urls", namespace="dash")),
    # Mounted unconditionally — the admin is the operator's observability window
    # and must not depend on DEBUG. It stays protected by Django auth (+ the
    # deploy's loopback bind + SSH tunnel); auto-login is loopback + flag gated
    # in ``teatree.core.middleware``.
    path("admin/", admin.site.urls),
    # Serve the admin's own static assets from the finders under a production
    # WSGI server (gunicorn) with DEBUG off — Django's ``runserver`` did this via
    # the dev static handler, which gunicorn does not wrap. ``insecure=True`` is
    # Django's sanctioned finder-serve for a single-operator loopback tool that
    # has no separate static server in front of it.
    re_path(r"^static/(?P<path>.*)$", serve_static, {"insecure": True}),
]
