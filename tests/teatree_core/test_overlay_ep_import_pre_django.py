"""Every ``teatree.overlays`` entry-point module must import before django.setup().

CLI assembly (``register_overlay_commands`` -> ``OverlayAppBuilder.build`` ->
``overlay_skill_metadata``) loads overlay entry points via
``overlay_loader._discover_overlays`` while building the Typer app — before any
command body runs ``ensure_django()``. A module-level ORM import in an overlay
module (or in any core module its import reaches) therefore kills every ``t3``
invocation, ``t3 doctor check`` included: ``AppRegistryNotReady`` when
``DJANGO_SETTINGS_MODULE`` is exported in the shell, ``ImproperlyConfigured``
when it is not. Both pre-setup interpreter states are driven here in fresh
subprocesses — the in-process suite can never catch this because pytest-django
runs ``django.setup()`` before any test module imports.
"""

import os
import subprocess
import sys
from importlib.metadata import entry_points

import pytest


def _entry_point_modules() -> list[str]:
    return sorted({ep.value.partition(":")[0] for ep in entry_points(group="teatree.overlays")})


def _pre_setup_env(**overrides: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k != "DJANGO_SETTINGS_MODULE"}
    env.update(overrides)
    return env


def _run_in_subprocess(code: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


@pytest.mark.integration
class TestOverlayEntryPointImportPreDjangoSetup:
    def test_every_entry_point_module_imports_without_django_settings(self) -> None:
        for module in _entry_point_modules():
            result = _run_in_subprocess(f"import {module}", _pre_setup_env())
            assert result.returncode == 0, f"{module} is not import-safe pre-django.setup():\n{result.stderr}"

    def test_every_entry_point_module_imports_with_settings_exported_but_apps_unpopulated(self) -> None:
        for module in _entry_point_modules():
            result = _run_in_subprocess(f"import {module}", _pre_setup_env(DJANGO_SETTINGS_MODULE="teatree.settings"))
            assert result.returncode == 0, f"{module} crashes when DJANGO_SETTINGS_MODULE is exported:\n{result.stderr}"


@pytest.mark.integration
class TestCliAssemblyPreDjangoSetup:
    def test_cli_assembles_with_settings_exported_but_apps_unpopulated(self) -> None:
        # The pre-setup state test_cli_tools does NOT cover: the operator's shell
        # exports DJANGO_SETTINGS_MODULE, so a pre-setup ORM import surfaces as
        # AppRegistryNotReady — which the overlay_skills fallback guard must
        # absorb for assembly to survive a broken overlay at all.
        driver = "import sys\nfrom teatree.cli import main\nsys.argv = ['t3', '--help']\nmain()\n"
        result = _run_in_subprocess(driver, _pre_setup_env(DJANGO_SETTINGS_MODULE="teatree.settings"))
        assert result.returncode == 0, result.stderr
