"""Tests for teatree.settings, teatree.urls, teatree.wsgi and teatree.__main__."""

import importlib
from unittest.mock import patch

from django.contrib.staticfiles.views import serve as serve_static
from django.test import override_settings
from django.urls import resolve, reverse

from teatree.settings import _debug_enabled


def test_settings_importable():
    """Importing the module should execute it without errors."""
    mod = importlib.import_module("teatree.settings")
    assert mod.SECRET_KEY == "teatree-dev-insecure"
    assert mod.DEBUG is True
    assert mod.USE_TZ is True
    assert mod.ROOT_URLCONF == "teatree.urls"
    assert "default" in mod.DATABASES
    assert mod.DATABASES["default"]["ENGINE"] == "django.db.backends.sqlite3"
    assert "teatree.core" in mod.INSTALLED_APPS
    assert "teatree.agents" in mod.INSTALLED_APPS
    assert "teatree.backends" in mod.INSTALLED_APPS
    assert isinstance(mod.LOGGING, dict)
    assert mod.LOGGING["version"] == 1
    assert mod.STATIC_URL == "static/"


def test_discover_overlay_apps_skips_broken_entry_points():
    """Entry points that raise on load are silently skipped."""
    broken_ep = type("FakeEP", (), {"load": lambda self: (_ for _ in ()).throw(ImportError("boom"))})()
    with patch("importlib.metadata.entry_points", return_value=[broken_ep]):
        mod = importlib.import_module("teatree.settings")
        result = mod._discover_overlay_apps()

    assert result == []


def test_main_module_sets_settings_and_delegates():
    """Importing __main__ should work and main() should set DJANGO_SETTINGS_MODULE."""
    mod = importlib.import_module("teatree.__main__")

    with patch("django.core.management.execute_from_command_line") as mock_exec:
        mod.main()

    mock_exec.assert_called_once()
    import os  # noqa: PLC0415

    assert os.environ["DJANGO_SETTINGS_MODULE"] == "teatree.settings"


def test_discover_overlay_apps_skips_entry_points_without_django_app():
    """Entry points whose class has no django_app attribute are skipped."""
    no_app_cls = type("NoApp", (), {})
    ep = type("FakeEP", (), {"load": lambda self: no_app_cls})()
    with patch("importlib.metadata.entry_points", return_value=[ep]):
        mod = importlib.import_module("teatree.settings")
        result = mod._discover_overlay_apps()

    assert result == []


def test_debug_defaults_on_and_env_disables_it(monkeypatch):
    """DEBUG is on by default (local-dev convenience) but T3_DEBUG=0 turns it off."""
    monkeypatch.delenv("T3_DEBUG", raising=False)
    assert _debug_enabled() is True

    monkeypatch.setenv("T3_DEBUG", "0")
    assert _debug_enabled() is False
    monkeypatch.setenv("T3_DEBUG", "false")
    assert _debug_enabled() is False
    monkeypatch.setenv("T3_DEBUG", "off")
    assert _debug_enabled() is False

    monkeypatch.setenv("T3_DEBUG", "1")
    assert _debug_enabled() is True
    monkeypatch.setenv("T3_DEBUG", "")
    assert _debug_enabled() is True


def test_admin_is_mounted_regardless_of_debug():
    """/admin/ mounts unconditionally — no longer gated on DEBUG (the deploy footgun)."""
    with override_settings(DEBUG=False):
        assert reverse("admin:index") == "/admin/"
    with override_settings(DEBUG=True):
        assert reverse("admin:index") == "/admin/"


def test_static_is_served_off_debug():
    """Admin static assets resolve to the finder-serve view even with DEBUG off.

    gunicorn does not wrap the app with runserver's dev static handler, so the
    urlconf serves static itself (`insecure=True`) — otherwise the admin renders
    unstyled under the production WSGI server.
    """
    with override_settings(DEBUG=False):
        match = resolve("/static/admin/css/base.css")
    assert match.func is serve_static
    assert match.kwargs.get("insecure") is True


def test_wsgi_application_is_a_callable():
    """teatree.wsgi exposes a WSGI `application` callable for gunicorn."""
    mod = importlib.import_module("teatree.wsgi")
    assert callable(mod.application)
