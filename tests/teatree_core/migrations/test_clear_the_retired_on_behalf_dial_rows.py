"""The ``0109`` data migration clears the stored rows under the retired on-behalf dial.

The branch that removed the field left rows in the measured box. This migration
clears them and carries a global immediate value onto the surviving preset control.

Anti-vacuous: dropping the ``RunPython`` leaves the rows in place and the first two tests
go RED; the last proves the cleanup is keyed on the literal key rather than sweeping the
table.
"""

from importlib import import_module

import pytest
from django.apps import apps as live_apps
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase

from teatree.core.models import ConfigSetting, Mode

_BEFORE = ("core", "0108_review_unrecordable_failure_kind")
_AFTER = ("core", "0109_clear_the_retired_on_behalf_dial_rows")

#: The migration's own clearing function.
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


class TestAnImmediateDialOpensEveryPreset(TestCase):
    """An "immediate" dial meant "post on my behalf at any hour", and egress is now its only home."""

    @staticmethod
    def _egress_after(scope: str) -> set[str]:
        Mode.objects.update_or_create(name="afk", defaults={"entries": {}, "egress": "forbid"})
        Mode.objects.update_or_create(name="maintenance", defaults={"entries": {}, "egress": "forbid"})
        ConfigSetting.objects.create(scope=scope, key="on_behalf_post_mode", value="immediate")

        clear_rows(live_apps, connection.schema_editor())

        return set(Mode.objects.filter(name__in=("afk", "maintenance")).values_list("egress", flat=True))

    def test_a_global_immediate_allows_egress_in_every_preset(self) -> None:
        assert self._egress_after("") == {"allow"}

    def test_an_overlay_scoped_immediate_cannot_open_a_box_global_preset(self) -> None:
        assert self._egress_after("acme-overlay") == {"forbid"}
