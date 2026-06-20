"""Regression tests for ``core.0093_collapse_agent_review_request_disabled``.

souliane/teatree#2579 item 1: the parallel side flag
``agent_review_request_disabled`` is deleted; review-request blocking is driven
off the autonomy TIER. The data migration maps each stale ``ConfigSetting`` row
keyed ``agent_review_request_disabled`` forward:

- a truthy row guard-sets that scope's ``autonomy = notify`` ONLY if no higher
    tier is already pinned for that scope (never downgrades a ``full`` overlay),
- then the stale row is DELETED in every case.

A falsy row carries no intent (the default already PROCEEDs) — it is just
deleted, never written as an autonomy tier.
"""

import importlib

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

# The migration module starts with a digit so it cannot be imported via the
# normal ``from ... import`` syntax; use ``importlib`` directly.
_migration_module = importlib.import_module(
    "teatree.core.migrations.0093_collapse_agent_review_request_disabled",
)


class CollapseReviewRequestFlagMigrationTest(TransactionTestCase):
    """0093 maps the stale flag onto ``autonomy = notify`` and deletes the row."""

    _BEFORE = ("core", "0092_remove_ticket_redis_db_index")
    _AFTER = ("core", "0093_collapse_agent_review_request_disabled")

    def _migrate(self, target: tuple[str, str]) -> "object":
        executor = MigrationExecutor(connection)
        executor.migrate([target])
        executor.loader.build_graph()
        return executor.loader.project_state([target]).apps

    def _restore_head(self) -> None:
        """Re-apply every core migration so TransactionTestCase teardown flushes the real schema."""
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        core_leaves = [node for node in executor.loader.graph.leaf_nodes() if node[0] == "core"]
        executor.migrate(core_leaves)

    def _set(self, apps: "object", *, key: str, value: object, scope: str) -> None:
        config_model = apps.get_model("core", "ConfigSetting")
        config_model.objects.update_or_create(scope=scope, key=key, defaults={"value": value})

    def _effective(self, apps: "object", *, key: str, scope: str) -> object | None:
        config_model = apps.get_model("core", "ConfigSetting")
        row = config_model.objects.filter(scope=scope, key=key).first()
        return row.value if row is not None else None

    def test_truthy_flag_maps_to_notify_and_deletes_stale_row(self) -> None:
        apps = self._migrate(self._BEFORE)
        self._set(apps, key="agent_review_request_disabled", value=True, scope="customer")

        _migration_module._collapse_flag(apps, connection.schema_editor())

        # The stale row is gone; autonomy=notify is set for that scope.
        assert self._effective(apps, key="agent_review_request_disabled", scope="customer") is None
        assert self._effective(apps, key="autonomy", scope="customer") == "notify"

        self._restore_head()

    def test_truthy_global_flag_maps_to_global_notify(self) -> None:
        apps = self._migrate(self._BEFORE)
        self._set(apps, key="agent_review_request_disabled", value=True, scope="")

        _migration_module._collapse_flag(apps, connection.schema_editor())

        assert self._effective(apps, key="agent_review_request_disabled", scope="") is None
        assert self._effective(apps, key="autonomy", scope="") == "notify"

        self._restore_head()

    def test_does_not_downgrade_a_full_overlay(self) -> None:
        apps = self._migrate(self._BEFORE)
        self._set(apps, key="autonomy", value="full", scope="solo")
        self._set(apps, key="agent_review_request_disabled", value=True, scope="solo")

        _migration_module._collapse_flag(apps, connection.schema_editor())

        # The stale row is deleted but the higher tier (full) is preserved.
        assert self._effective(apps, key="agent_review_request_disabled", scope="solo") is None
        assert self._effective(apps, key="autonomy", scope="solo") == "full"

        self._restore_head()

    def test_preserves_an_existing_notify_pin(self) -> None:
        apps = self._migrate(self._BEFORE)
        self._set(apps, key="autonomy", value="notify", scope="cust")
        self._set(apps, key="agent_review_request_disabled", value=True, scope="cust")

        _migration_module._collapse_flag(apps, connection.schema_editor())

        assert self._effective(apps, key="agent_review_request_disabled", scope="cust") is None
        assert self._effective(apps, key="autonomy", scope="cust") == "notify"

        self._restore_head()

    def test_falsy_flag_is_deleted_without_setting_autonomy(self) -> None:
        apps = self._migrate(self._BEFORE)
        self._set(apps, key="agent_review_request_disabled", value=False, scope="cust")

        _migration_module._collapse_flag(apps, connection.schema_editor())

        # A falsy row carries no intent — deleted, and no autonomy row is written.
        assert self._effective(apps, key="agent_review_request_disabled", scope="cust") is None
        assert self._effective(apps, key="autonomy", scope="cust") is None

        self._restore_head()

    def test_babysit_scope_is_upgraded_to_notify(self) -> None:
        apps = self._migrate(self._BEFORE)
        self._set(apps, key="autonomy", value="babysit", scope="cust")
        self._set(apps, key="agent_review_request_disabled", value=True, scope="cust")

        _migration_module._collapse_flag(apps, connection.schema_editor())

        # babysit is a lower tier than notify, so the truthy flag upgrades it.
        assert self._effective(apps, key="agent_review_request_disabled", scope="cust") is None
        assert self._effective(apps, key="autonomy", scope="cust") == "notify"

        self._restore_head()
