"""Serve a dashboard checkout for the visual QA browser in the same network namespace."""

import os
import sqlite3
import sys
from importlib import import_module
from pathlib import Path

import django
from django.conf import settings
from django.core.management import call_command

import teatree
from teatree.visual_qa_server import DATABASE_OVERRIDES, SCRATCH_PREFIX


def main() -> None:
    repo = Path(__file__).resolve().parents[2]
    imported = Path(teatree.__file__).resolve().parent
    expected = (repo / "src" / "teatree").resolve()
    if imported != expected:
        message = f"teatree imported from {imported}; expected checkout at {expected}"
        raise SystemExit(message)
    if os.environ.get("DJANGO_SETTINGS_MODULE") != "e2e.dash.settings":
        message = "visual QA requires e2e.dash.settings"
        raise SystemExit(message)
    if any(os.environ.get(key) for key in DATABASE_OVERRIDES):
        message = "visual QA refuses inherited database overrides"
        raise SystemExit(message)
    scratch = os.environ.get("XDG_DATA_HOME", "")
    if scratch != os.environ.get("TMPDIR") or not Path(scratch).name.startswith(SCRATCH_PREFIX):
        message = "visual QA requires an isolated scratch directory"
        raise SystemExit(message)

    sys.path.insert(0, str(repo))
    django.setup()
    with sqlite3.connect(settings.DATABASES["default"]["NAME"], uri=True):
        call_command("migrate", run_syncdb=True, interactive=False, verbosity=0)
        import_module("e2e.dash.seed").seed_board()
        port = int(os.environ["T3_VISUAL_QA_PORT"])
        call_command("runserver", f"127.0.0.1:{port}", use_reloader=False, verbosity=0)


if __name__ == "__main__":
    main()
