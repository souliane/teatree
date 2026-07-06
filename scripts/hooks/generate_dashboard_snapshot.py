"""Pre-commit hook: regenerate the admin-dashboard HTML snapshot from the models.

Renders ``teatree.core.factory.dashboard_snapshot`` against an isolated test database and
writes the canonical ``docs/generated/dashboard/admin-index.html``. Auto-stages the
result unless ``DASHBOARD_SNAPSHOT_NO_STAGE`` is set — CI sets it for the docs-drift
step so the ``git diff --exit-code docs/generated`` gate (the same entrypoint the FSM
diagrams use) catches a stale committed snapshot. Modelled on
``generate_fsm_diagrams.py``; the only added machinery is the throwaway test DB the
admin index needs (auth/session/log tables) that a pure-introspection generator does
not.

See: souliane/teatree#12
"""

import os
import subprocess
import sys
from pathlib import Path

_CANONICAL = Path("docs/generated/dashboard/admin-index.html")


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def django_setup() -> None:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "teatree.settings")
    src = repo_root() / "src"
    if src.is_dir():
        sys.path.insert(0, str(src))
    import django

    django.setup()


def render_in_test_db() -> str:
    from django.db import connection
    from django.test.utils import setup_test_environment, teardown_test_environment

    from teatree.core.factory.dashboard_snapshot import render_dashboard_snapshot

    setup_test_environment()
    config = connection.creation.create_test_db(verbosity=0, autoclobber=True)
    try:
        return render_dashboard_snapshot()
    finally:
        connection.creation.destroy_test_db(config, verbosity=0)
        teardown_test_environment()


def main() -> int:
    django_setup()
    html = render_in_test_db()
    path = repo_root() / _CANONICAL
    if path.is_file() and path.read_text(encoding="utf-8") == html:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")
    if not os.environ.get("DASHBOARD_SNAPSHOT_NO_STAGE"):
        subprocess.run(["git", "add", str(path)], check=False)
        print(f"Updated {_CANONICAL}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
