"""Deterministic render of the teatree admin dashboard to a byte-stable HTML string.

The user-facing HTML dashboard was removed in #541; the Django admin index is the
remaining server-rendered HTML surface and a faithful "always-fresh screenshot" of
teatree's domain models — register a model in ``core/admin.py`` and this snapshot
gains a row. The render is the role ``core/diagrams.py`` plays for the FSM diagrams:
pure-output logic that the generator hook and the drift gate both call.

Determinism is the whole contract (a flapping snapshot reds CI), so every volatile
input is frozen rather than captured live. A dedicated ``AdminSite`` carries only
``teatree.core``'s registered model admins, so installed overlays and the ``auth`` /
``django_tasks`` admin entries cannot make the model list vary by machine. The
output-affecting settings (``ROOT_URLCONF``, ``STATIC_URL``, ``LANGUAGE_CODE``,
``TIME_ZONE``, ``DEBUG``) are pinned, so the script context (default settings) and
the pytest context (test settings, ``DEBUG`` off, an overlay app installed) render
identically. A fixed superuser and an empty log table give a stable greeting and
"Recent actions → None available."; the per-request CSRF token is stripped.

This module doubles as its own URLconf (``urlpatterns`` mounts the dedicated site),
so the render routes through it via ``override_settings(ROOT_URLCONF=__name__)``
on its own dedicated ``AdminSite``, independent of the project's ``/admin/`` route.

See: souliane/teatree#12
"""

import re

from django.apps import apps
from django.contrib import admin
from django.contrib.admin import AdminSite
from django.contrib.auth import get_user_model
from django.test import Client
from django.test.utils import override_settings
from django.urls import path

_DASHBOARD_USER = "teatree"
_CSRF_VALUE = re.compile(r'(name="csrfmiddlewaretoken"\s+value=")[^"]*(")')


def _build_dashboard_site() -> AdminSite:
    admin.autodiscover()
    site = AdminSite(name="teatree_dashboard")
    site.site_header = "TeaTree"
    site.site_title = "TeaTree dashboard"
    site.index_title = "Domain models"
    for model in apps.get_app_config("core").get_models():
        if admin.site.is_registered(model):
            # get_model_admin is the public Django 5.0+ getter; bundled stubs lag it.
            registered = admin.site.get_model_admin(model)  # ty: ignore[unresolved-attribute]
            site.register(model, type(registered))
    return site


dashboard_site = _build_dashboard_site()
urlpatterns = [path("", dashboard_site.urls)]


def _canonical_html(html: str) -> str:
    """Freeze the per-request CSRF token and strip per-line trailing whitespace.

    The trailing-whitespace strip keeps the committed snapshot stable under the
    repo's ``trailing-whitespace`` pre-commit hook, which would otherwise rewrite
    the generated file and drift it from a fresh render.
    """
    frozen = _CSRF_VALUE.sub(r"\1CSRF\2", html)
    return "\n".join(line.rstrip() for line in frozen.split("\n"))


def render_dashboard_snapshot() -> str:
    """Render the teatree admin index to deterministic HTML (requires a usable DB).

    The caller owns the database: under pytest it is the per-test transaction; the
    generator hook wraps the call in an isolated test database.
    """
    user_model = get_user_model()
    user, _ = user_model.objects.get_or_create(
        username=_DASHBOARD_USER,
        defaults={"is_staff": True, "is_superuser": True, "is_active": True},
    )
    with override_settings(
        ROOT_URLCONF=__name__,
        STATIC_URL="/static/",
        LANGUAGE_CODE="en-us",
        TIME_ZONE="UTC",
        DEBUG=False,
    ):
        client = Client()
        client.force_login(user)
        html = client.get("/").content.decode("utf-8")
    return _canonical_html(html)
