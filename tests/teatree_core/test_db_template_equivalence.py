"""Equivalence test: the migrated-DB template restore lane matches a fresh migrate (W7-PR2).

The session-scoped ``django_db_setup`` override in ``tests/conftest.py`` restores
a byte-for-byte SQLite ``backup()`` snapshot of the first worker's freshly
migrated DB into every LATER worker's in-memory test DB, instead of re-running
``migrate``. Whichever lane produced THIS worker's DB (build or restore), these
assertions pin that the result is indistinguishable from a real migrate: every
default loop seeded with the sound-on set enabled, a faithful ``django_migrations``
history (load-bearing for ``test_initial_migration_seed.py``'s reverse migrate),
and the ``TEST["MIGRATE"]`` override reset back to its default ``True`` once
setup completes.
"""

from django.db import connection
from django.db.migrations.recorder import MigrationRecorder
from django.test import TestCase

from teatree.core.models import Loop
from teatree.loops.seed import DEFAULT_LOOPS
from tests._db_template import schema_hash, template_path

_SOUND_ON = frozenset(spec.name for spec in DEFAULT_LOOPS if spec.default_enabled)


class TestDbTemplateEquivalence(TestCase):
    def test_template_file_exists_for_the_current_schema(self) -> None:
        assert template_path(schema_hash()).exists(), (
            "django_db_setup must have published (or restored from) a template for the current schema_hash()"
        )

    def test_seeds_every_default_loop_with_the_sound_on_set_enabled(self) -> None:
        assert Loop.objects.count() == len(DEFAULT_LOOPS)
        enabled = set(Loop.objects.filter(enabled=True).values_list("name", flat=True))
        assert enabled == _SOUND_ON

    def test_django_migrations_is_faithful_and_includes_the_head_migration(self) -> None:
        applied = MigrationRecorder(connection).applied_migrations()
        core_migrations = {name for app, name in applied if app == "core"}
        assert core_migrations, (
            "django_migrations must carry a real record — FreshMigrateSeedsDefaultLoops "
            "depends on it for its 'migrate core zero' reverse migrate"
        )
        assert "0001_initial" in core_migrations
        assert "0015_dreamqaprobe_scope" in core_migrations

    def test_migrate_setting_is_reset_to_true_after_setup(self) -> None:
        # The restore lane temporarily sets TEST["MIGRATE"] = False to skip the
        # RunPython seed (run_syncdb only), then must reset it in a finally so
        # every OTHER connection user sees Django's documented default.
        assert connection.settings_dict["TEST"]["MIGRATE"] is True
