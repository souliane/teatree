"""Tests for public API — every public __init__.py defines __all__."""

import importlib

import pytest

# Packages that must define __all__ (public API surface).
# Django-internal namespaces (management, commands, migrations, templates)
# are excluded — they have no user-facing API.
PUBLIC_PACKAGES = [
    "teatree",
    "teatree.agents",
    "teatree.backends",
    "teatree.cli",
    "teatree.contrib",
    "teatree.contrib.t3_teatree",
    "teatree.core",
    "teatree.core.models",
    "teatree.overlay_init",
    "teatree.utils",
]


@pytest.mark.parametrize("package_name", PUBLIC_PACKAGES)
def test_public_package_defines_all(package_name: str) -> None:
    mod = importlib.import_module(package_name)
    assert hasattr(mod, "__all__"), f"{package_name} must define __all__"
    assert isinstance(mod.__all__, list), f"{package_name}.__all__ must be a list"


@pytest.mark.parametrize("package_name", PUBLIC_PACKAGES)
def test_all_entries_are_importable(package_name: str) -> None:
    mod = importlib.import_module(package_name)
    if not hasattr(mod, "__all__"):
        pytest.skip(f"{package_name} has no __all__ yet")
    for name in mod.__all__:
        assert hasattr(mod, name), f"{package_name}.__all__ lists '{name}' but it is not defined"
