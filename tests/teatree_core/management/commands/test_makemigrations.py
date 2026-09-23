"""``teatree.core`` shadows ``makemigrations`` so its system checks name no database.

Django 6.1 checks every configured database when the caller names none, so the
JSONField support check connects before ``handle()`` runs — and CI's lint venue,
which runs ``makemigrations --check --dry-run`` as the migration-graph linearity
gate, has no openable database file.
"""

from django.core.management import get_commands, load_command_class
from django_linear_migrations.management.commands.makemigrations import Command as LinearMigrationsMakeMigrations

from teatree.core.management.commands.makemigrations import Command as TeatreeMakeMigrations


def test_the_shadow_wins_the_command_lookup_and_names_no_database() -> None:
    resolved = load_command_class(get_commands()["makemigrations"], "makemigrations")

    assert isinstance(resolved, TeatreeMakeMigrations), (
        f"teatree.core must precede django_linear_migrations in INSTALLED_APPS; got {type(resolved)}"
    )
    assert resolved.get_check_kwargs({})["databases"] == [], "the shadow's system checks must name no database"


def test_the_shadow_keeps_the_dlm_bookkeeping() -> None:
    """Subclassing Django's command directly would silently stop the bookkeeping.

    It resolves and runs fine, and stops writing ``max_migration.txt`` — the
    sentinel every dlm.E001-E005 error reads.
    """
    resolved = load_command_class(get_commands()["makemigrations"], "makemigrations")

    assert isinstance(resolved, LinearMigrationsMakeMigrations), (
        "the shadow must subclass django-linear-migrations' makemigrations, or max_migration.txt stops being written"
    )
