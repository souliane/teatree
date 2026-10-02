"""``0085`` moves the sweep scope off the preset rows onto one box setting.

The value has to survive the move, and the migration has to REFUSE rather than pick when
the presets disagree — a divergence means per-preset scoping was actually in use, which is
an owner decision about what this box sweeps, not something a migration may settle by
reading whichever row comes first.
"""

import pytest
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

_BEFORE = ("core", "0084_red_mr_fix_attempt_review_findings")
_AFTER = ("core", "0085_scanner_overlay_scope_leaves_the_preset")
_SETTING = "scanner_overlay_scope"


@pytest.mark.timeout(240)
class TestScannerOverlayScopeLeavesThePreset(TransactionTestCase):
    def setUp(self) -> None:
        self.addCleanup(self._restore_head)

    @staticmethod
    def _restore_head() -> None:
        connection.close()
        call_command("migrate", "core", "--no-input", verbosity=0)

    def _seed_presets(self, scopes: dict[str, list[str]]) -> None:
        executor = MigrationExecutor(connection)
        executor.migrate([_BEFORE])
        mode = executor.loader.project_state(_BEFORE).apps.get_model("core", "Mode")
        mode.objects.all().delete()
        for name, scope in scopes.items():
            mode.objects.create(name=name, entries={}, overlay_scope=scope)

    def _apply(self):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate([_AFTER])
        return MigrationExecutor(connection).loader.project_state([_AFTER]).apps

    def _stored_scope(self, apps) -> list[str] | None:
        row = apps.get_model("core", "ConfigSetting").objects.filter(key=_SETTING, scope="").first()
        return row.value if row is not None else None

    def test_a_uniform_scope_becomes_the_box_setting(self) -> None:
        self._seed_presets({"present": ["primary"], "afk": ["primary"], "off": ["primary"]})
        assert self._stored_scope(self._apply()) == ["primary"]

    def test_an_unscoped_box_seeds_nothing_because_the_shipped_default_already_means_all(self) -> None:
        self._seed_presets({"present": [], "afk": []})
        assert self._stored_scope(self._apply()) is None

    def test_a_preset_that_never_scoped_does_not_outvote_the_ones_that_did(self) -> None:
        # An empty scope is the ABSENCE of an opinion, not a competing one — otherwise a
        # newly-created preset would silently widen the sweep for the whole box.
        self._seed_presets({"present": ["primary"], "newborn": []})
        assert self._stored_scope(self._apply()) == ["primary"]

    def test_diverging_presets_refuse_rather_than_pick_one(self) -> None:
        self._seed_presets({"present": ["primary"], "afk": ["secondary"]})
        with pytest.raises(RuntimeError, match="presets disagree"):
            self._apply()
        # The refusal leaves the box mid-migration by design, so make the rows agree
        # before the cleanup migrates to head — otherwise it refuses again and the
        # teardown, not the assertion, is what the report names.
        self._seed_presets({"present": ["primary"], "afk": ["primary"]})
