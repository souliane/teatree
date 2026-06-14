"""Pre-commit hook: django-linear-migrations system check must pass.

Runs ``manage.py check --tag models`` so Django's check framework fires
``check_max_migration_files`` (registered by ``django_linear_migrations``).
This catches forked migration graphs (dlm.E005), merge-conflict residue in
``max_migration.txt`` (dlm.E002), missing ``max_migration.txt`` (dlm.E001),
and stale ``max_migration.txt`` (dlm.E003/E004) at commit time.

Exit code 0 = clean, 1 = check failure or unexpected error.
"""

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "manage.py"), "check", "--tag", "models"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(result.stdout, end="")
        print(result.stderr, end="", file=sys.stderr)
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
