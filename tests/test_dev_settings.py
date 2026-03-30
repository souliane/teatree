"""Tests for teatree.settings and teatree.__main__."""

import importlib
from unittest.mock import patch


def test_settings_importable():
    """Importing the module should execute it without errors."""
    mod = importlib.import_module("teatree.settings")
    assert mod.SECRET_KEY == "teatree-dev-insecure"
    assert mod.DEBUG is True
    assert mod.USE_TZ is True
    assert mod.ROOT_URLCONF == "teatree.core.urls"
    assert "default" in mod.DATABASES
    assert mod.DATABASES["default"]["ENGINE"] == "django.db.backends.sqlite3"
    assert "teatree.core" in mod.INSTALLED_APPS
    assert "teatree.agents" in mod.INSTALLED_APPS
    assert isinstance(mod.LOGGING, dict)
    assert mod.LOGGING["version"] == 1
    assert mod.TEATREE_HEADLESS_RUNTIME == "claude-code"
    assert mod.TEATREE_INTERACTIVE_RUNTIME == "codex"
    assert mod.TEATREE_TERMINAL_MODE == "same-terminal"
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
