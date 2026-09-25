"""The ``0109`` data migration clears the stored rows under the retired on-behalf dial.

The branch that removed the field left the rows: ``ConfigSetting.set_value`` never
refused the key, so two overlay-scope rows survive on the measured box and the resolver
prints a loud stderr line naming the key on EVERY resolution — and that resolution is on
the statusline/hook/gate hot path.

Anti-vacuous: dropping the ``RunPython`` leaves the rows in place and the first two tests
go RED; the last proves the cleanup is keyed on the literal key rather than sweeping the
table.
"""

import io
from contextlib import redirect_stderr
from importlib import import_module

import pytest
from django.apps import apps as live_apps
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase

from teatree.config import get_effective_settings
from teatree.core.models import ConfigSetting

_BEFORE = ("core", "0108_review_unrecordable_failure_kind")
_AFTER = ("core", "0109_clear_the_retired_on_behalf_dial_rows")

#: The migration's OWN clearing function, so the warning proof is about the shipped
#: code path rather than a hand-rolled delete that resembles it.
clear_rows = import_module(f"teatree.core.migrations.{_AFTER[1]}").clear_rows


@pytest.mark.timeout(240)
class TestClearTheRetiredOnBehalfDialRows(TransactionTestCase):
    def setUp(self) -> None:
        self.addCleanup(self._restore_head)

    @staticmethod
    def _restore_head() -> None:
        connection.close()
        call_command("migrate", "core", "--no-input", verbosity=0)

    @staticmethod
    def _seed_before(rows: tuple[tuple[str, str, str], ...]) -> None:
        executor = MigrationExecutor(connection)
        executor.migrate([_BEFORE])
        config_setting = executor.loader.project_state(_BEFORE).apps.get_model("core", "ConfigSetting")
        config_setting.objects.all().delete()
        for scope, key, value in rows:
            config_setting.objects.create(scope=scope, key=key, value=value)

    @staticmethod
    def _migrate_and_read() -> dict[tuple[str, str], str]:
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate([_AFTER])
        config_setting = (
            MigrationExecutor(connection).loader.project_state([_AFTER]).apps.get_model("core", "ConfigSetting")
        )
        return {(row.scope, row.key): row.value for row in config_setting.objects.all()}

    def test_the_measured_overlay_scope_rows_are_cleared(self) -> None:
        self._seed_before(
            (
                ("acme-overlay", "on_behalf_post_mode", '"immediate"'),
                ("t3-teatree", "on_behalf_post_mode", '"immediate"'),
            )
        )

        assert self._migrate_and_read() == {}

    def test_a_row_away_from_the_old_shipped_default_is_cleared_too(self) -> None:
        """The field is gone, so the row is inert whatever it holds — see the migration."""
        self._seed_before((("", "on_behalf_post_mode", '"immediate"'),))

        assert self._migrate_and_read() == {}

    def test_a_live_key_is_untouched(self) -> None:
        self._seed_before((("", "wip", '"slow"'),))

        assert self._migrate_and_read() == {("", "wip"): '"slow"'}


class TestTheWarningIsWhatStops(TestCase):
    """The row is not noise in the abstract: it warns on EVERY resolution.

    ``get_effective_settings`` is memoized only inside a ``request_scope``, and the
    statusline/hook/gate callers each resolve outside one — so the stderr line is paid
    per call. Seeding the row and counting the lines is what makes "the migration clears
    it" mean "the warning stops".
    """

    @staticmethod
    def _warnings_over(calls: int, overlay: str) -> int:
        captured = io.StringIO()
        with redirect_stderr(captured):
            for _ in range(calls):
                get_effective_settings(overlay)
        return sum(1 for line in captured.getvalue().splitlines() if "'on_behalf_post_mode' was removed" in line)

    def test_a_seeded_row_warns_once_per_resolution_until_the_migration_clears_it(self) -> None:
        ConfigSetting.objects.create(scope="acme-overlay", key="on_behalf_post_mode", value='"immediate"')
        assert self._warnings_over(3, "acme-overlay") == 3

        clear_rows(live_apps, connection.schema_editor())

        assert not ConfigSetting.objects.filter(key="on_behalf_post_mode").exists()
        assert self._warnings_over(3, "acme-overlay") == 0
