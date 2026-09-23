"""Pre-commit hook: django-linear-migrations system check must pass.

Runs the ``models``-tagged system checks so ``check_max_migration_files``
(registered by ``django_linear_migrations``) fires. This catches forked
migration graphs (dlm.E005), merge-conflict residue in ``max_migration.txt``
(dlm.E002), missing ``max_migration.txt`` (dlm.E001), and stale
``max_migration.txt`` (dlm.E003/E004) at commit time.

The graph guard reads only the migration files on disk, so it names no
database: Django 6.1 checks every configured database when the caller names
none, which opens a connection the lint venue has no writable DB file for.

Exit code 0 = clean, 1 = check failure or unexpected error.
"""

import sys

from django.core.checks import Tags
from django.core.management import call_command
from django.core.management.base import SystemCheckError

from teatree.core.management.db_free_checks import without_database_checks
from teatree.utils.django_bootstrap import ensure_django


def main() -> int:
    ensure_django()
    try:
        call_command("check", **without_database_checks({"tags": [Tags.models]}))
    except SystemCheckError as failure:
        print(failure, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
