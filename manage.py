#!/usr/bin/env python3
import os
import sys
from pathlib import Path


def main() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "teatree.contrib.t3_teatree.settings")
    from django.core.management import execute_from_command_line  # noqa: PLC0415

    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
