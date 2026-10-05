"""The ``deploy_roll`` command defers its Django-heavy roller until it runs."""

import os
import subprocess
import sys
from pathlib import Path

_CORE_ROOT = Path(__file__).resolve().parents[4]
_PROBE = """
import sys
import django
django.setup()
import teatree.core.management.commands.deploy_roll
print("teatree.deploy.roll" in sys.modules)
"""


def test_discovering_the_command_does_not_import_the_roller() -> None:
    env = {**os.environ, "PYTHONPATH": str(_CORE_ROOT), "DJANGO_SETTINGS_MODULE": "tests.django_settings"}

    result = subprocess.run(
        [sys.executable, "-c", _PROBE], capture_output=True, text=True, env=env, check=True, timeout=120
    )

    assert result.stdout.strip().splitlines()[-1] == "False"
