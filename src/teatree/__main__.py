"""Allow ``python -m teatree <command>`` as a manage.py equivalent.

Works for pip-installed teatree without a repo checkout or ``uv``.
"""

import os
import sys


def main() -> None:  # pragma: no cover — console-script entry point (Django dispatch glue)
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "teatree.settings")
    from django.core.management import execute_from_command_line  # noqa: PLC0415 — deferred: Django import at call time

    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
