from django.contrib import admin
from django.contrib.staticfiles.storage import staticfiles_storage
from django.contrib.staticfiles.views import serve as serve_static
from django.urls import include, path, re_path
from django.views.generic.base import RedirectView

urlpatterns = [
    # Browsers request /favicon.ico unprompted on any page that declares no icon
    # link. The dash templates declare one; Django's admin does not, so the admin
    # index 404'd on every real-browser load (the console guard's response listener
    # records that as an error). Serving it at the site root covers every page,
    # declared link or not, and shares the ONE brand mark the dash base template
    # loads.
    path(
        "favicon.ico",
        RedirectView.as_view(url=staticfiles_storage.url("dash/favicon.svg"), permanent=True),
        name="favicon",
    ),
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
