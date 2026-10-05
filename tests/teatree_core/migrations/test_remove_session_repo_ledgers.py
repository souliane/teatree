"""Migration 0127 retires digest models and their auth metadata; its reverse restores no data."""

import importlib

import pytest
from django.apps.registry import Apps
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

_BEFORE = ("core", "0126_name_the_worker_generation_and_cursor_tables")
_AFTER = ("core", "0127_remove_session_repo_ledgers")
_RETIRED = ("dailydigestmessage", "dailydigestthread")
_MIGRATION = importlib.import_module("teatree.core.migrations.0127_remove_session_repo_ledgers")


@pytest.mark.timeout(240)
class TestRemoveSessionRepoLedgers(TransactionTestCase):
    def setUp(self) -> None:
        self.addCleanup(self._restore_head)

    @staticmethod
    def _restore_head() -> None:
        connection.close()
        call_command("migrate", "core", "--no-input", verbosity=0)

    def test_retired_content_types_and_permissions_are_deleted_after_migrate(self) -> None:
        executor = MigrationExecutor(connection)
        executor.migrate([_BEFORE])
        metadata_targets = [node for node in executor.loader.graph.leaf_nodes() if node[0] in {"auth", "contenttypes"}]
        assert {node[0] for node in metadata_targets} == {"auth", "contenttypes"}
        historical_apps = executor.loader.project_state([_BEFORE, *metadata_targets]).apps
        content_type = historical_apps.get_model("contenttypes", "ContentType")
        permission = historical_apps.get_model("auth", "Permission")
        retired_ids = []
        permission_ids = []
        for model in _RETIRED:
            row, _ = content_type.objects.get_or_create(app_label="core", model=model)
            retired_ids.append(row.pk)
            item, _ = permission.objects.get_or_create(
                content_type=row,
                codename=f"view_{model}",
                defaults={"name": f"Can view {model}"},
            )
            permission_ids.append(item.pk)
        retained, _ = content_type.objects.get_or_create(app_label="core", model="retained_metadata")
        retained_permission, _ = permission.objects.get_or_create(
            content_type=retained,
            codename="view_retained_metadata",
            defaults={"name": "Can view retained metadata"},
        )

        MigrationExecutor(connection).migrate([_AFTER])
        assert not content_type.objects.filter(pk__in=retired_ids).exists()
        assert not permission.objects.filter(pk__in=permission_ids).exists()
        assert content_type.objects.filter(pk=retained.pk).exists()
        assert permission.objects.filter(pk=retained_permission.pk).exists()

    def test_cleanup_allows_an_install_without_contenttypes_or_auth(self) -> None:
        _MIGRATION.delete_retired_digest_content_types(Apps(installed_apps=[]), None)

    def test_reverse_restores_mode_on_existing_send_audit_rows(self) -> None:
        executor = MigrationExecutor(connection)
        executor.migrate([_BEFORE])
        send_audit = executor.loader.project_state([_BEFORE]).apps.get_model("core", "SendAudit")
        row = send_audit.objects.create(channel="slack", allowlist_verdict="allowed", mode="enforce")

        MigrationExecutor(connection).migrate([_AFTER])
        MigrationExecutor(connection).migrate([_BEFORE])

        restored = MigrationExecutor(connection).loader.project_state([_BEFORE]).apps.get_model("core", "SendAudit")
        assert restored.objects.get(pk=row.pk).mode == ""

    def test_reverse_marks_every_restored_chat_row_consumed_never_pending(self) -> None:
        # A restored column is empty, and an empty consumed_at reads as a chat line still to inject.
        executor = MigrationExecutor(connection)
        executor.migrate([_BEFORE])
        pending = executor.loader.project_state([_BEFORE]).apps.get_model("core", "PendingChatInjection")
        row = pending.objects.create(channel="C", slack_ts="1.0", text="hello")

        MigrationExecutor(connection).migrate([_AFTER])
        migrated = (
            MigrationExecutor(connection).loader.project_state([_AFTER]).apps.get_model("core", "PendingChatInjection")
        )
        assert "consumed_at" not in {field.name for field in migrated._meta.fields}
        MigrationExecutor(connection).migrate([_BEFORE])

        restored = (
            MigrationExecutor(connection).loader.project_state([_BEFORE]).apps.get_model("core", "PendingChatInjection")
        )
        assert restored.objects.get(pk=row.pk).consumed_at is not None
        assert not restored.objects.filter(consumed_at__isnull=True).exists()

    def test_cleanup_allows_an_install_without_auth(self) -> None:
        executor = MigrationExecutor(connection)
        contenttypes_target = next(node for node in executor.loader.graph.leaf_nodes() if node[0] == "contenttypes")
        historical_apps = executor.loader.project_state([contenttypes_target]).apps
        historical_apps.get_model("contenttypes", "ContentType")
        with pytest.raises(LookupError):
            historical_apps.get_model("auth", "Permission")
        _MIGRATION.delete_retired_digest_content_types(historical_apps, None)
