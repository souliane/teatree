"""Tests for teetree.dev_settings — just import to execute the settings module."""

import importlib


def test_dev_settings_importable():
    """Importing the module should execute it without errors."""
    mod = importlib.import_module("teetree.dev_settings")
    assert mod.SECRET_KEY == "teatree-dev-insecure"
    assert mod.DEBUG is True
    assert mod.USE_TZ is True
    assert mod.ROOT_URLCONF == "teetree.core.urls"
    assert "default" in mod.DATABASES
    assert mod.DATABASES["default"]["ENGINE"] == "django.db.backends.sqlite3"
    assert "teetree.core" in mod.INSTALLED_APPS
    assert "teetree.agents" in mod.INSTALLED_APPS
    assert isinstance(mod.LOGGING, dict)
    assert mod.LOGGING["version"] == 1
    assert mod.TEATREE_HEADLESS_RUNTIME == "claude-code"
    assert mod.TEATREE_INTERACTIVE_RUNTIME == "codex"
    assert mod.TEATREE_TERMINAL_MODE == "same-terminal"
    assert mod.STATIC_URL == "static/"
