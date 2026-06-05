"""Regression tests for ``core.0054_canonicalize_legacy_overlay_names`` (#1959).

0054 generalizes 0027: every distinct overlay value that ``resolve_overlay_name``
folds onto a *different* registered canonical is renamed (collision twins deleted
first); a value that resolves to itself or to nothing is left untouched. The
poison-pill guard then permanently fails the genuinely-unresolvable rows.
"""

import importlib
from unittest.mock import patch

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone

_migration_module = importlib.import_module("teatree.core.migrations.0054_canonicalize_legacy_overlay_names")

# Deterministic resolver: legacy aliases fold onto canonical entry points;
# a genuinely-unknown value resolves to None and must be left untouched.
_RESOLUTIONS = {
    "teatree": "t3-teatree",
    "beta": "t3-beta",
    "t3-teatree": "t3-teatree",
    "t3-beta": "t3-beta",
}


def _fake_resolve(name: str) -> str | None:
    return _RESOLUTIONS.get(name)


class CanonicalizeLegacyOverlayMigrationTest(TransactionTestCase):
    _BEFORE = ("core", "0053_planartifact")
    _AFTER = ("core", "0054_canonicalize_legacy_overlay_names")

    def _migrate(self, target: tuple[str, str]) -> "object":
        executor = MigrationExecutor(connection)
        executor.migrate([target])
        executor.loader.build_graph()
        return executor.loader.project_state([target]).apps

    def _restore_head(self) -> None:
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        core_leaves = [node for node in executor.loader.graph.leaf_nodes() if node[0] == "core"]
        executor.migrate(core_leaves)

    def test_legacy_alias_is_renamed_to_registered_canonical(self) -> None:
        apps = self._migrate(self._BEFORE)
        ticket = apps.get_model("core", "Ticket")
        legacy = ticket.objects.create(overlay="teatree", issue_url="scanning-news://teatree")

        with patch("teatree.core.overlay_loader.resolve_overlay_name", _fake_resolve):
            _migration_module._canonicalize_legacy_overlay_names(apps, connection.schema_editor())

        legacy.refresh_from_db()
        assert legacy.overlay == "t3-teatree"

        self._restore_head()

    def test_unresolvable_overlay_is_left_untouched(self) -> None:
        apps = self._migrate(self._BEFORE)
        ticket = apps.get_model("core", "Ticket")
        synthetic = ticket.objects.create(overlay="removed-overlay", issue_url="architectural-review://removed-overlay")

        with patch("teatree.core.overlay_loader.resolve_overlay_name", _fake_resolve):
            _migration_module._canonicalize_legacy_overlay_names(apps, connection.schema_editor())

        synthetic.refresh_from_db()
        assert synthetic.overlay == "removed-overlay"

        self._restore_head()

    def test_colliding_legacy_row_is_merged_into_canonical_twin(self) -> None:
        apps = self._migrate(self._BEFORE)
        pending = apps.get_model("core", "PendingChatInjection")
        now = timezone.now()
        canonical = pending.objects.create(
            overlay="t3-teatree", channel="C1", slack_ts="1700000000.000100", text="canonical", received_at=now
        )
        legacy = pending.objects.create(
            overlay="teatree", channel="C1", slack_ts="1700000000.000100", text="legacy", received_at=now
        )

        with patch("teatree.core.overlay_loader.resolve_overlay_name", _fake_resolve):
            _migration_module._canonicalize_legacy_overlay_names(apps, connection.schema_editor())

        survivors = list(pending.objects.filter(slack_ts="1700000000.000100"))
        assert len(survivors) == 1
        assert survivors[0].pk == canonical.pk
        assert survivors[0].overlay == "t3-teatree"
        assert not pending.objects.filter(pk=legacy.pk).exists()

        self._restore_head()

    def test_non_colliding_alias_is_canonicalized(self) -> None:
        apps = self._migrate(self._BEFORE)
        ticket = apps.get_model("core", "Ticket")
        legacy = ticket.objects.create(overlay="beta", issue_url="scanning-news://beta")

        with patch("teatree.core.overlay_loader.resolve_overlay_name", _fake_resolve):
            _migration_module._canonicalize_legacy_overlay_names(apps, connection.schema_editor())

        legacy.refresh_from_db()
        assert legacy.overlay == "t3-beta"

        self._restore_head()
