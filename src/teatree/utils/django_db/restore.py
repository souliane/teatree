"""Dump-source validation for the restore strategies.

The importer's local-dump and CI-dump strategies filter candidate dumps
through :func:`validate_dump` before paying the multi-minute ``pg_restore``.
"""

import sys
from pathlib import Path

from teatree.utils.run import run_allowed_to_fail


def validate_dump(dump_path: Path) -> bool:
    if dump_path.stat().st_size == 0:
        return False
    result = run_allowed_to_fail(["pg_restore", "-l", str(dump_path)], expected_codes=None)
    if "could not read" in (result.stderr or ""):
        sys.stdout.write(f"  WARNING: Dump appears truncated: {dump_path.name} (delete and re-fetch)\n")
        return False
    return True
