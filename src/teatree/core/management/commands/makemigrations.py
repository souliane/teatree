"""``makemigrations`` answers from the migration files on disk, so its checks name no database.

Django 6.1 checks every configured database when the caller names none, so the
JSONField support check connects before ``handle()`` runs — and CI's lint venue,
which runs ``makemigrations --check --dry-run`` as the migration-graph linearity
gate, has no openable database file. Subclasses ``django_linear_migrations``'
own override rather than Django's, because this app is later in
``INSTALLED_APPS`` and would otherwise silently displace its
``max_migration.txt`` bookkeeping.
"""

from typing import Any

from django_linear_migrations.management.commands.makemigrations import Command as LinearMigrationsCommand

from teatree.core.management.db_free_checks import without_database_checks


class Command(LinearMigrationsCommand):
    def get_check_kwargs(self, options: dict[str, Any]) -> dict[str, Any]:
        return without_database_checks(super().get_check_kwargs(options))
